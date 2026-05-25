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

# install zig 0.16.0 (stable)
RUN curl -L https://ziglang.org/download/0.16.0/zig-x86_64-linux-0.16.0.tar.xz | tar -xJ -C /usr/local \
    && ln -s /usr/local/zig-x86_64-linux-0.16.0/zig /usr/local/bin/zig

# rclone (pinned) for R2 transport — handles multipart upload (>5GiB) +
# streaming download + retries. Used by the indexer (upload) and App B
# (download); the ingester role doesn't use it (TODO: split images by role).
RUN curl -fsSL https://downloads.rclone.org/v1.69.1/rclone-v1.69.1-linux-amd64.zip -o /tmp/rclone.zip \
    && unzip -j /tmp/rclone.zip 'rclone-*/rclone' -d /usr/local/bin \
    && chmod +x /usr/local/bin/rclone \
    && rm /tmp/rclone.zip

WORKDIR /app
COPY build.zig build.zig.zon build.zig.zon.gh ./
COPY src ./src

# tangled.org (primary dep host) 502s intermittently and zig has no per-dep
# fallback URL, so a failed fetch falls back to GitHub mirrors (same content,
# same hashes — see build.zig.zon.gh). Primary path is unchanged.
RUN zig build -Doptimize=ReleaseSafe \
    || ( echo "primary dep fetch failed (tangled.org?); retrying via GitHub mirrors" \
         && cp build.zig.zon.gh build.zig.zon \
         && zig build -Doptimize=ReleaseSafe )

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
COPY --from=builder /usr/local/bin/rclone /usr/local/bin/rclone

CMD ["./typeahead-ingester"]
