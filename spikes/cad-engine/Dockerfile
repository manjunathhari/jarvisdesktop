# Spike image: the CADCraft headless tool and nothing else. Throwaway; not part of the JARVIS server.
# The binary is copied in by the workflow after its checksum is verified.
# TRACE=1 adds strace, used once to count network connection attempts.
FROM debian:trixie-slim
ARG TRACE=0
RUN if [ "$TRACE" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends strace && rm -rf /var/lib/apt/lists/*; \
    fi
COPY cadcraft-cli /usr/local/bin/cadcraft-cli
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/cadcraft-cli"]
