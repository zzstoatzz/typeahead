# build stage
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    # prefer IPv4 — tangled.org has flaky IPv6 that truncates tarball fetches
    && echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf

# install zig 0.16.0 (stable)
RUN curl -L https://ziglang.org/download/0.16.0/zig-x86_64-linux-0.16.0.tar.xz | tar -xJ -C /usr/local \
    && ln -s /usr/local/zig-x86_64-linux-0.16.0/zig /usr/local/bin/zig

WORKDIR /app
COPY build.zig build.zig.zon ./
COPY src ./src

RUN zig build -Doptimize=ReleaseSafe

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

CMD ["./typeahead-ingester"]
