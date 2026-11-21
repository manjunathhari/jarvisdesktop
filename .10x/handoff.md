# Handoff
**Now:** Architecture v2 Phase 0. JARVIS server is **running** on the shared Hostinger server (Docker project `jarvis`, deployed at commit b525bd3). P0-1…P0-5 passed live (init SMOKE lines), P0-6 in CI.

**Remaining Phase 0**
- **P0-8:** PASS 21 Nov (network-check.yml; weekly re-run).
- **P0-7:** deferred by Manj until the build goes big (Hostinger weekly backup suffices for now). Still write the rebuild/move runbook (`server/deploy/README.md`).
- **T0.8:** QA + security review → `.10x/reviews/`; mark PR #2 ready; Manj approves merge.

**How to deploy a new version:** push to `phase-0-foundations`/`main` → CI publishes `ghcr.io/manjunathhari/jarvis-server:<commit>` → connector `vps_docker_create` with content = raw GitHub URL of `server/deploy/compose.yaml` at that commit and environment `JARVIS_REF=<commit>` → read `vps_docker_logs` for SMOKE lines.

**Constraints:** build workspace cannot reach the server over SSH; connector has no shell. Do not touch pubmedgemini, Traefik, Ollama or the Postgres 16 project.
