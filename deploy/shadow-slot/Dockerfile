# Build context is the repository root:
#   docker build --platform linux/amd64 -f deploy/shadow-slot/Dockerfile -t typeahead-shadow-slot:local .
# Every stage is linux/amd64. On an arm64 host the whole build runs emulated.

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS zig

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    python3 \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# same compiler and checksum as scripts/ci-zig.sh
ARG ZIG_VERSION=0.17.0
ARG ZIG_SHA256=1cbe9df9f27e6b78d14ccbca43b6703a404ef79ef1c463de901d7f088d4e2026
RUN curl -fsSL "https://ziglang.org/download/${ZIG_VERSION}/zig-x86_64-linux-${ZIG_VERSION}.tar.xz" -o /tmp/zig.tar.xz \
    && echo "${ZIG_SHA256}  /tmp/zig.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/zig.tar.xz -C /usr/local \
    && rm /tmp/zig.tar.xz \
    && ln -s "/usr/local/zig-x86_64-linux-${ZIG_VERSION}/zig" /usr/local/bin/zig

FROM zig AS store

WORKDIR /src/services
COPY services/build.zig services/build.zig.zon ./
COPY services/src ./src
# the vendored tarballs seed the package cache, as in services/Dockerfile;
# refresh them with scripts/vendor-zig-deps.py after a dep bump
COPY services/.zig-pkg-vendor /root/.cache/zig/p/
# the handle quarantine list the store role embeds; services/build.zig reads
# it from ../src
COPY src/identity-quarantine.json /src/src/identity-quarantine.json

# -Dtarget pins the x86-64 baseline: native CPU detection breaks under
# emulation (Rosetta reports an unknown CPU model)
RUN --network=none zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu

FROM zig AS normalizer

WORKDIR /src
COPY services/src/index services/src/index
COPY services/src/search.zig services/src/search.zig
COPY services/tantivy-normalizer/adapter.zig services/tantivy-normalizer/build.py services/tantivy-normalizer/
RUN --network=none mkdir /out \
    && python3 services/tantivy-normalizer/build.py --target x86_64-linux-gnu --output /out/libtypeahead_normalizer.a

FROM rust:1.91.1-slim-bookworm@sha256:8514999d4786ef12efe89239e86b3d0a021b94b9d35108c8efe6c79ca7dc1a65 AS index

WORKDIR /src
COPY services/tantivy/Cargo.toml services/tantivy/Cargo.lock services/tantivy/build.rs services/tantivy/
COPY services/tantivy/src services/tantivy/src
COPY src/identity-quarantine.json src/moderation-regexes.json src/
RUN cargo fetch --locked --target x86_64-unknown-linux-gnu --manifest-path services/tantivy/Cargo.toml

COPY --from=normalizer /out/libtypeahead_normalizer.a /normalizer/
ENV TA_NORMALIZER_LIB_DIR=/normalizer
RUN --network=none cargo build --release --locked --offline --manifest-path services/tantivy/Cargo.toml

# prints the index's stored actors as JSON lines for the store's bootstrap
FROM rust:1.91.1-slim-bookworm@sha256:8514999d4786ef12efe89239e86b3d0a021b94b9d35108c8efe6c79ca7dc1a65 AS dump

WORKDIR /src
COPY experiments/zkv-scale/dump-actors/Cargo.toml experiments/zkv-scale/dump-actors/Cargo.lock ./
COPY experiments/zkv-scale/dump-actors/src ./src
RUN cargo build --release --locked

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    jq \
    rclone \
    && rm -rf /var/lib/apt/lists/* \
    # prefer IPv4 over IPv6 for outbound connections, as the ingester image does
    && echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf

WORKDIR /app
COPY --from=store /src/services/zig-out/bin/typeahead-ingester .
COPY --from=index /src/services/tantivy/target/release/typeahead-tantivy .
COPY --from=dump /src/target/release/dump-actors .
COPY --chmod=0755 deploy/shadow-slot/supervise deploy/shadow-slot/bootstrap.sh ./

EXPOSE 8080
CMD ["/app/supervise"]
