# Handoff
**Now:** Architecture v2 Phase 0. JARVIS server is **running** on the shared Hostinger server (Docker project `jarvis`, deployed at commit b525bd3). P0-1…P0-5 passed live (init SMOKE lines), P0-6 in CI.

**Remaining Phase 0**
- **P0-8:** `.github/workflows/network-check.yml` scans from a GitHub runner. Needs repo secret `SERVER_IP` (Manj adds it in GitHub → Settings → Secrets and variables → Actions); then re-run the workflow.
- **P0-7 (T0.7):** first daily dump lands after midnight IST in volume `jarvis_backups`. Drill plan: deploy a one-shot Docker project via the connector that mounts that volume (external), restores the newest dump into a scratch Postgres 17 and compares the P0-1 smoke ticket and its 7 audit rows; read the result from its logs. Write the rebuild/move runbook (`server/deploy/README.md`).
- **T0.8:** QA + security review → `.10x/reviews/`; mark PR #2 ready; Manj approves merge.

**How to deploy a new version:** push to `phase-0-foundations`/`main` → CI publishes `ghcr.io/manjunathhari/jarvis-server:<commit>` → connector `vps_docker_create` with content = raw GitHub URL of `server/deploy/compose.yaml` at that commit and environment `JARVIS_REF=<commit>` → read `vps_docker_logs` for SMOKE lines.

**Constraints:** build workspace cannot reach the server over SSH; connector has no shell. Do not touch pubmedgemini, Traefik, Ollama or the Postgres 16 project.
