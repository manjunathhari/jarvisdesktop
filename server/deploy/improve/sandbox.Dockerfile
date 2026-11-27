# JARVIS improve sandbox (compose profile "improve"): where the Claude Agent SDK works on a
# checkout of this repo, one stage per container. It holds no key of any kind: Claude is reached
# through claude-proxy (ANTHROPIC_BASE_URL), which adds the team's key, and the web only through
# improve-egress (HTTPS_PROXY). Python 3.12 + uv for the server, Rust stable and Node 22 for the
# desktop app, with the Linux libraries the desktop CI installs (ci.yml).
#
# Built from server/ (CI: docker build -f deploy/improve/sandbox.Dockerfile .). Every image is
# pinned by digest; rustup-init is checked against its published SHA-256 before it runs.
FROM node:22.23.5-trixie-slim@sha256:f0960f8dc047f358e3fb428f36ccba21732194fa5f0cac849f401e63e62fb9e6 AS node

FROM python:3.12.16-slim-trixie@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

COPY --from=ghcr.io/astral-sh/uv:0.9.30@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    RUSTUP_HOME=/opt/rustup \
    PATH="/opt/cargo/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      file \
      git \
      pkg-config \
      libwebkit2gtk-4.1-dev \
      libayatana-appindicator3-dev \
      librsvg2-dev \
      libxdo-dev \
      libssl-dev \
      libasound2-dev \
 && rm -rf /var/lib/apt/lists/*

# Node 22 for the desktop UI: the Tauri crate needs app/dist at compile time (npm run build).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# Rust stable, minimal profile, plus the components CI uses (rustfmt, clippy).
ARG RUSTUP_VERSION=1.29.3
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) triple=x86_64-unknown-linux-gnu; sum=b1c8cb14e30889e7e2999e6b69e62d9e9b9ffc0f4df8180564d306d9680e6c55 ;; \
      arm64) triple=aarch64-unknown-linux-gnu; sum=9f608c142e0e38a78957d0661ba1dfaf0b6fb37184cfaa4b9a0aa57af6184282 ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/rustup-init \
      "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/${triple}/rustup-init"; \
    echo "${sum}  /tmp/rustup-init" | sha256sum -c -; \
    chmod +x /tmp/rustup-init; \
    CARGO_HOME=/opt/cargo /tmp/rustup-init -y --no-modify-path --profile minimal \
      --default-toolchain stable --component rustfmt,clippy; \
    rm /tmp/rustup-init; \
    cargo --version && rustc --version

# The Claude Agent SDK in its own venv, at an exact version (the wheel bundles the Claude Code
# CLI). The venv also sees this image's copy of the server code (a .pth file), so the runner
# imports; nothing else does, so the checkout's own tests never pick up this copy.
RUN python -m venv /opt/agent-sdk \
 && /opt/agent-sdk/bin/pip install --no-cache-dir claude-agent-sdk==0.2.189
COPY src /opt/jarvis/src
RUN echo /opt/jarvis/src > "$(/opt/agent-sdk/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/jarvis_server_src.pth" \
 && /opt/agent-sdk/bin/python -c "import claude_agent_sdk, jarvis_server" \
 && groupadd --system --gid 10001 jarvis \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/jarvis --no-create-home jarvis \
 && install -d -o 10001 -g 10001 -m 0700 /work \
 && install -d -m 1777 /cache /home/jarvis

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/opt/agent-sdk/bin/python", "-m", "jarvis_server.improve.sandbox_runner"]
