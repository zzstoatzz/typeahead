# build stage
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xz-utils \
    unzip \
    && rm -rf /var/lib/apt/lists/* \
    # prefer IPv4 — tangled.org has flaky IPv6 that truncates tarball fetches
    && echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf

# install zig 0.17.0 (stable)
RUN curl -L https://ziglang.org/download/0.17.0/zig-x86_64-linux-0.17.0.tar.xz | tar -xJ -C /usr/local \
    && ln -s /usr/local/zig-x86_64-linux-0.17.0/zig /usr/local/bin/zig

# rclone (pinned) for R2 transport — handles multipart upload (>5GiB) +
# streaming download + retries. Used by the indexer (upload) and search service
# (download); the ingester role doesn't use it (TODO: split images by role).
RUN curl -fsSL https://downloads.rclone.org/v1.69.1/rclone-v1.69.1-linux-amd64.zip -o /tmp/rclone.zip \
    && unzip -j /tmp/rclone.zip 'rclone-*/rclone' -d /usr/local/bin \
    && chmod +x /usr/local/bin/rclone \
    && rm /tmp/rclone.zip

WORKDIR /app
COPY build.zig build.zig.zon ./
COPY src ./src

# seed the zig package cache from the vendored dep tarballs so the build
# needs no dep fetches at all — tangled.org 503s zig's fetch client
# (observed 2026-08-14: depot builder and local docker both blocked while
# plain curl succeeded). Refresh with scripts/vendor-zig-deps.py after any
# dep bump in build.zig.zon.
COPY .zig-pkg-vendor /root/.cache/zig/p/

# -Dtarget pins the x86-64 baseline explicitly: native CPU detection breaks
# under emulated local builds (Rosetta reports an unknown CPU model)
RUN --network=none zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu

# runtime stage
FROM debian:bookworm-slim

# sqlite3 baked in so debug sessions can query /data/local.db without
# paying the apt-install tax on every fly ssh; ca-certificates for HTTPS.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    # prefer IPv4 over IPv6 for outbound connections
    && echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf

WORKDIR /app
COPY --from=builder /app/zig-out/bin/typeahead-ingester .
COPY --from=builder /app/zig-out/bin/admit-prefix .
COPY --from=builder /usr/local/bin/rclone /usr/local/bin/rclone

CMD ["./typeahead-ingester"]
