# JARVIS server — runbook

How to run, update, rebuild and move the JARVIS server. Design: `.10x/specs/2026-11-21-phase-0-foundations-design.md`, ADR-006…009; memory engine: `.10x/specs/2026-11-21-phase-1-memory-engine-design.md`, ADR-010.

**The principle (K5):** losing ledger data is acceptable; losing JARVIS is not. Everything needed to recreate JARVIS is in this repository. Rebuilding on a new server and moving off the current one use the same steps.

No addresses, hostnames or secrets belong in this file (the repository is public).

---

## 1. What runs where

| Piece | Where |
|---|---|
| Code, migrations, compose file, hardening script | This repo (`server/`) |
| Image | `ghcr.io/manjunathhari/jarvis-server:<commit>`, built and published by CI (`.github/workflows/server.yml`) after the tests pass on `main` or `phase-0-foundations` |
| Secrets | `/etc/jarvis/secrets/` on the server (root:10001, 0440), created by `harden/harden.sh`. Never in the repo, never in Hostinger's environment field |
| Data | Docker volume `jarvis_pgdata` (Postgres 17) |
| Daily dumps | Docker volume `jarvis_backups` (`last/`, `daily/`, `weekly/`, `monthly/`; newest is `last/jarvis-latest.sql.gz`) |
| Memory | Docker volume `jarvis_memory`: a private git repo of markdown files, mounted only into `core`. **No git remote, ever** — core refuses to start if one exists |
| Memory backups | Docker volume `jarvis_memory_backups`: one `memory-YYYY-MM-DD.bundle` per day, newest 7 kept (written by `core`) |
| Admin access | `tailscale ssh root@jarvis-server` from Manj's PC, or the Hostinger web console |

Containers (Docker project `jarvis`): `postgres`, `init` (one-shot), `core`, `scheduler`, `backup`. No port is published; all sit on the internal network `jarvis_internal`.

## 2. Deploy a new version

1. Merge or push to `main` (or `phase-0-foundations`). Wait for the **Server** workflow: `test` and `publish` must both be green.
2. Deploy that commit (`<sha>` = full commit SHA):
   - **Through the Hostinger connector** (how Claude deploys): `vps_docker_create` with project name `jarvis`, content `https://raw.githubusercontent.com/manjunathhari/jarvisdesktop/<sha>/server/deploy/compose.yaml`, environment `JARVIS_REF=<sha>`.
   - **By hand on the server:**
     ```bash
     mkdir -p /opt/jarvis && cd /opt/jarvis
     curl -fsSLo compose.yaml https://raw.githubusercontent.com/manjunathhari/jarvisdesktop/<sha>/server/deploy/compose.yaml
     echo "JARVIS_REF=<sha>" > .env
     docker compose up -d
     ```
3. Check the `init` logs. Every deploy runs migrations and then prints `SMOKE PASS …` lines: P0-1…P0-5, the memory audit function, and M1…M4 (ten lines). The memory checks run in a temporary repo with fictional people; they never touch `jarvis_memory` or write audit rows. `core` and `scheduler` start only if `init` succeeded. `core` then logs `memory ready: N entries, head <commit>`.
   ```bash
   docker logs jarvis-init-1 | grep SMOKE
   ```
   Each smoke run leaves a few rows labelled `source_ref = 'smoke-check'` in the ledger. To turn the smoke off for a deploy, add `JARVIS_SMOKE_ON_INIT=0` to the environment.

**Roll back:** deploy the previous commit the same way. Migrations only move forward on deploy; if a migration must be undone, use `jarvis-core downgrade` deliberately (see §6), never as part of a routine rollback.

## 3. Build a new server from scratch (or move to JARVIS's own server)

1. **New server:** Ubuntu 24.04, 2 vCPU / 4–8 GB is plenty. Docker Engine with the compose plugin installed.
2. **Harden it:** in the server's console, as root, run the hardening script pinned to a commit and checked by fingerprint (get both from the latest commit that changed `server/deploy/harden/harden.sh`):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/manjunathhari/jarvisdesktop/<sha>/server/deploy/harden/harden.sh -o /root/harden.sh \
     && echo "<sha256>  /root/harden.sh" | sha256sum -c && bash /root/harden.sh
   ```
   Approve the Tailscale link it prints. It creates fresh database passwords in `/etc/jarvis/secrets/`.
3. **Firewall at the provider:** on a server of its own, allow **only** UDP 41641 (Tailscale) — no 22, 80 or 443. (On the shared server, 22/80/443 stay open for pubmedgemini.)
4. **Deploy** as in §2. An empty JARVIS is a valid result; stop here if no data needs to move.
5. **Bring the data across (optional)** — §4 (ledger) and §7 (memory).
6. **Verify:** `SMOKE PASS` ×10 in the `init` logs and `memory ready` in the `core` logs; run the **Server network check** workflow after updating the `SERVER_IP` repository secret and the expected port list in `.github/workflows/network-check.yml`.
7. **Retire the old copy:** stop the old project (`docker compose down`, or delete project `jarvis` through the connector). Keep its volumes until the new server has run for a week.

## 4. Move or restore the data

**Take a fresh dump on the old server** (or use the newest daily dump):
```bash
# fresh, custom format
docker exec jarvis-postgres-1 pg_dump -U postgres -Fc jarvis > /root/jarvis.dump
# or the newest daily dump (plain SQL, gzip)
docker run --rm -v jarvis_backups:/b alpine cat /b/last/jarvis-latest.sql.gz > /root/jarvis-latest.sql.gz
```
Copy the file to the new server over Tailscale (`tailscale file cp`, or `scp` to `root@<new-server>` on the tailnet).

**Restore on the new server, before the app starts:**
```bash
cd /opt/jarvis
docker compose up -d postgres
docker compose run --rm init bootstrap        # roles + empty database, no migrations
# custom-format dump:
docker exec -i jarvis-postgres-1 pg_restore -U postgres -d jarvis --exit-on-error < /root/jarvis.dump
# or plain daily dump:
gunzip -c /root/jarvis-latest.sql.gz | docker exec -i jarvis-postgres-1 psql -U postgres -d jarvis -v ON_ERROR_STOP=1
docker compose up -d                          # init now finds migrations at head, runs smoke
```
The dump carries the Alembic version table, so `init` only applies migrations that are newer than the dump.

**Restore drill (P0-7, deferred by Manj until the build grows):** restore the newest daily dump into a scratch container and compare the latest smoke ticket:
```bash
docker run -d --name jarvis-drill --network none -e POSTGRES_PASSWORD=drill postgres:17
sleep 10
docker exec jarvis-drill psql -U postgres -c "CREATE ROLE jarvis_owner; CREATE ROLE jarvis_app; CREATE DATABASE jarvis OWNER jarvis_owner;"
docker run --rm -v jarvis_backups:/b alpine cat /b/last/jarvis-latest.sql.gz | gunzip | docker exec -i jarvis-drill psql -U postgres -d jarvis -q
docker exec jarvis-drill psql -U postgres -d jarvis -c "SELECT count(*) AS audit_rows FROM audit.event WHERE entity_id = (SELECT id FROM ledger.task WHERE title = 'P0-1 test ticket' ORDER BY created_at DESC LIMIT 1);"   # expect 7
docker rm -f jarvis-drill
```

## 5. Everyday checks

| Question | Command (on the server) |
|---|---|
| Is everything up? | `docker ps --filter label=com.docker.compose.project=jarvis` |
| Recent logs | `docker logs --tail 50 jarvis-core-1` (also `-scheduler-1`, `-init-1`, `-backup-1`) |
| Run the live checks again | `docker exec jarvis-core-1 jarvis-core smoke` |
| Silent processes | `docker exec jarvis-postgres-1 psql -U postgres -d jarvis -c "SELECT * FROM ops.heartbeat"` |
| Last backups | `docker run --rm -v jarvis_backups:/b alpine ls -l /b/last /b/daily` |
| Memory loaded? | `docker logs jarvis-core-1 2>&1 \| grep 'memory ready'` |
| Last memory bundles | `docker run --rm -v jarvis_memory_backups:/b alpine ls -l /b` |
| fail2ban working | `fail2ban-client status sshd` |
| Tailscale | `tailscale status` |

## 6. Rarely needed

`docker compose` commands need the compose file. Deployed by hand it is `/opt/jarvis/compose.yaml`; deployed through the Hostinger connector, find it with
`docker inspect jarvis-core-1 --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}'` and run the commands from that folder (or pass `-f <file>`).


- **Run a single admin command:** `docker compose run --rm init <bootstrap|migrate|downgrade|smoke>` (the `init` service has the owner and superuser secrets).
- **Undo the newest migration deliberately:** `docker compose run --rm init downgrade` — only after taking a dump (§4). Migration 0007 refuses to go down once memory audit rows exist: the audit trail is append-only.
- **Rotate the database passwords:** replace the files in `/etc/jarvis/secrets/` (keep root:10001, 0440), then `docker compose up -d` (init re-applies them to the roles). The Postgres superuser password lives in the data volume; change it with `ALTER ROLE postgres PASSWORD …` to match the new file.
- **Hard reset (data loss accepted):** `docker compose down -v` deletes `jarvis_pgdata`, `jarvis_backups`, `jarvis_memory` and `jarvis_memory_backups`; the next `up` builds an empty JARVIS. **Before real memories exist, this is harmless; afterwards, copy a memory bundle off the server first.**

## 7. Memory (Phase 1)

Phase 1 holds **no real personal memories** (Manj, 2026-11-22). Real memories arrive only after JARVIS moves to its own server (§3); encryption at rest follows as later hardening.

**If `core` will not start and its log names a memory file and line** (`MemoryFormatError: people/x.md:7: …`), a file was edited by hand or damaged. Either fix that line to the format in the design spec §4.3, or restore the newest bundle (below). Do not delete the volume.

**If `core` says the repo has a remote** (`RemoteConfigured`), someone added one. Remove it — memory must never be pushable:
```bash
docker run --rm -v jarvis_memory:/m --user 10001 alpine/git -C /m remote remove origin
```

**Restore memory from a bundle** (also how memory moves to a new server):
```bash
docker compose stop core
docker run --rm -v jarvis_memory_backups:/b -v jarvis_memory:/m --user 10001 --entrypoint sh alpine/git \
  -c 'rm -rf /m/* /m/.git && git clone -q /b/memory-<date>.bundle /m && git -C /m remote remove origin'
docker compose start core          # logs "memory ready" and reconciles the audit trail
```
To move memory to another server, copy one `.bundle` file across (it holds the whole history) and run the same restore there.

