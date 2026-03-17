# build stage
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# install zig 0.15.2
RUN curl -L https://ziglang.org/download/0.15.2/zig-x86_64-linux-0.15.2.tar.xz | tar -xJ -C /usr/local \
    && ln -s /usr/local/zig-x86_64-linux-0.15.2/zig /usr/local/bin/zig

WORKDIR /app
COPY build.zig build.zig.zon ./
COPY src ./src

RUN zig build -Doptimize=ReleaseSafe

# runtime stage
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # prefer IPv4 over IPv6 for outbound connections
    && echo 'precedence ::ffff:0:0/96  100' >> /etc/gai.conf

WORKDIR /app
COPY --from=builder /app/zig-out/bin/typeahead-ingester .

CMD ["./typeahead-ingester"]
