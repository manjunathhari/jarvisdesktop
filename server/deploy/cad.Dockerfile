# JARVIS `cad` image: the CAD engine (CADCraft `cadcraft-cli`, pinned) and its runner, nothing else.
# Build from the repository root:
#   docker build -f server/deploy/cad.Dockerfile -t jarvis-cad .
# Run locked down (compose: service `cad`): internal network only, read-only, /tmp as tmpfs,
# all capabilities dropped, non-root, 256 MB. The runner gives the engine no environment.
# There are no ARGs in this file.
# Plan: .10x/plans/2026-11-28-cad-board-slice-1.md, Task 2.

# The base image tags below float (they move with upstream releases); they can be pinned by
# digest later (FROM image:tag@sha256:...) if builds must be reproducible.

# ---- the runner, built from this repository
FROM rust:1-slim-trixie AS runner
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p jarvis-cad-runner --bin jarvis-cad-runner

# ---- the engine: the pinned release, checked against its SHA-256 before it is unpacked
FROM debian:trixie-slim AS engine
# The engine is fixed here, not by build arguments: no --build-arg can change what is fetched.
# Version 0.12.0; SHA-256 from the release's SHA256SUMS.txt, read on 27 Nov 2026. A different
# file fails the build.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    file="cadcraft-0.12.0-linux-x86_64.tar.gz"; \
    mkdir -p /download /unpack; \
    curl -fsSL --retry 3 -o "/download/${file}" \
      "https://github.com/storytold/cadcraft/releases/download/v0.12.0/${file}"; \
    cd /download; \
    echo "6ae27b9eabc3608caef356a00f69b9fdccbf1e408d6308b5614b6e6b256308de  ${file}" | sha256sum -c -; \
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
# Container mode: the runner is pid 1 here (run it without an init: --container is refused
# unless the runner is pid 1); after every replay it kills every other process, reaps them and
# empties /tmp. If /tmp cannot be emptied it exits (code 3) after answering, so the service
# needs a restart policy (Task 7) to come back with a fresh tmpfs.
ENTRYPOINT ["/usr/local/bin/jarvis-cad-runner", "--container", "--listen", "0.0.0.0:7411", "--engine", "/usr/local/bin/cadcraft-cli", "--commands", "/etc/jarvis/cad_commands.txt", "--work-root", "/tmp"]
