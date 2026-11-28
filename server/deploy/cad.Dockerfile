# JARVIS `cad` image: the CAD engine (CADCraft `cadcraft-cli`, pinned) and its runner, nothing else.
# Build from the repository root:
#   docker build -f server/deploy/cad.Dockerfile -t jarvis-cad .
# Run locked down (compose: service `cad`): internal network only, read-only, /tmp as tmpfs,
# all capabilities dropped, non-root, 256 MB. The runner gives the engine no environment.
# Plan: .10x/plans/2026-11-28-cad-board-slice-1.md, Task 2.

# ---- the runner, built from this repository
FROM rust:1-slim-trixie AS runner
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p jarvis-cad-runner --bin jarvis-cad-runner

# ---- the engine: the pinned release, checked against its SHA-256 before it is unpacked
FROM debian:trixie-slim AS engine
ARG CADCRAFT_VERSION=0.12.0
# From the release's SHA256SUMS.txt, read on 27 Nov 2026. A different file fails the build.
ARG CADCRAFT_SHA256=6ae27b9eabc3608caef356a00f69b9fdccbf1e408d6308b5614b6e6b256308de
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    file="cadcraft-${CADCRAFT_VERSION}-linux-x86_64.tar.gz"; \
    mkdir -p /download /unpack; \
    curl -fsSL --retry 3 -o "/download/${file}" \
      "https://github.com/storytold/cadcraft/releases/download/v${CADCRAFT_VERSION}/${file}"; \
    cd /download; \
    echo "${CADCRAFT_SHA256}  ${file}" | sha256sum -c -; \
    tar -xzf "/download/${file}" -C /unpack; \
    cli="$(find /unpack -type f -name cadcraft-cli | head -n 1)"; \
    test -n "$cli"; \
    install -m 0755 "$cli" /cadcraft-cli

# ---- the image that runs
FROM debian:trixie-slim
COPY --from=engine /cadcraft-cli /usr/local/bin/cadcraft-cli
COPY --from=runner /src/target/release/jarvis-cad-runner /usr/local/bin/jarvis-cad-runner
COPY server/config/cad_commands.txt /etc/jarvis/cad_commands.txt
USER 10001:10001
EXPOSE 7411
ENTRYPOINT ["/usr/local/bin/jarvis-cad-runner", "--listen", "0.0.0.0:7411", "--engine", "/usr/local/bin/cadcraft-cli", "--commands", "/etc/jarvis/cad_commands.txt"]
