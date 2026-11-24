# JARVIS server — runbook

How to run, update, rebuild and move the JARVIS server. Design: `.10x/specs/2026-11-21-phase-0-foundations-design.md`, ADR-006…009; memory engine: `.10x/specs/2026-11-21-phase-1-memory-engine-design.md`, ADR-010; gateway, nodes and brain: `.10x/specs/2026-11-22-phase-2-core-and-nodes-design.md`, ADR-011…014 (§8 below).

**The principle (K5):** losing ledger data is acceptable; losing JARVIS is not. Everything needed to recreate JARVIS is in this repository. Rebuilding on a new server and moving off the current one use the same steps.

No addresses, hostnames or secrets belong in this file (the repository is public).

---

## 1. What runs where

| Piece | Where |
|---|---|
| Code, migrations, compose file, hardening script | This repo (`server/`) |
| Image | `ghcr.io/manjunathhari/jarvis-server:<commit>`, built and published by CI (`.github/workflows/server.yml`) after the tests pass on `main` or `phase-0-foundations` |
| Secrets | `/etc/jarvis/secrets/` on the server (root:10001, 0440), created by `harden/harden.sh`: four database passwords, the core and gateway Ed25519 signing keys (and `gateway_signing_key.pub`), and two provided secrets, `tailscale_gateway_authkey` and `anthropic_api_key` (§8.1). Never in the repo, never in Hostinger's environment field |
| Data | Docker volume `jarvis_pgdata` (Postgres 17) |
| Daily dumps | Docker volume `jarvis_backups` (`last/`, `daily/`, `weekly/`, `monthly/`; newest is `last/jarvis-latest.sql.gz`) |
| Memory | Docker volume `jarvis_memory`: a private git repo of markdown files, mounted only into `core`. **No git remote, ever** — core refuses to start if one exists |
| Memory backups | Docker volume `jarvis_memory_backups`: one `memory-YYYY-MM-DD.bundle` per day, newest 7 kept (written by `core`) |
| Admin access | `tailscale ssh root@jarvis-server` from Manj's PC, or the Hostinger web console |
| Gateway on the tailnet | Device `jarvis-gw` (tag `tag:jarvis-gateway`), port 8443, run by the `gateway-ts` sidecar; its state is in volume `jarvis_gateway_ts_state` |

Containers (Docker project `jarvis`): `postgres`, `init` (one-shot), `core`, `scheduler`, `backup`, `gateway`, `gateway-ts`, `egress-proxy`. No port is published (CI fails the build if one appears). All sit on the internal network `jarvis_internal`, which has no internet route. Two also have one: `gateway-ts` (network `jarvis_ts_egress`, for Tailscale only) and `egress-proxy` (network `jarvis_egress`, which allows only `CONNECT api.anthropic.com:443`). The `gateway` runs inside `gateway-ts`'s network namespace, and the sidecar's entrypoint lets the gateway's UID out only on `tailscale0`, to `jarvis_internal`, and to loopback (§4.2).

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
3. Check the `init` logs. Every deploy runs migrations and then prints `SMOKE PASS …` lines: P0-1…P0-5, the memory audit function, M1…M4, and the eight database-only Phase 2 rows P2-E01, P2-E04, P2-E06, P2-E11, P2-S1, P2-S6-db, P2-B-idx and P2-fence (eighteen lines). The memory checks run in a temporary repo with fictional people; they never touch `jarvis_memory` or write audit rows. The P2 rows use the fictional person "Smoke Fictional" and a throwaway smoke node, which they revoke at the end; they need no node, gateway or brain. `core`, `scheduler` and `gateway` start only if `init` succeeded. `core` then logs `memory ready: N entries, head <commit>` and `Claude brain wired: model …`.
   ```bash
   docker logs jarvis-init-1 | grep SMOKE
   ```
   Each smoke run leaves a few rows labelled `source_ref = 'smoke-check'` in the ledger, and a revoked smoke node with terminal outbox items for "Smoke Fictional". To turn the smoke off for a deploy, add `JARVIS_SMOKE_ON_INIT=0` to the environment.
4. **First Phase 2 deploy only:** record the trusted keys (§8.2). Until then the `gateway` refuses to start and keeps restarting.

**Roll back:** deploy the previous commit the same way. Migrations only move forward on deploy; if a migration must be undone, use `jarvis-core downgrade` deliberately (see §6), never as part of a routine rollback.

## 3. Build a new server from scratch (or move to JARVIS's own server)

1. **New server:** Ubuntu 24.04, 2 vCPU / 4–8 GB is plenty. Docker Engine with the compose plugin installed.
2. **Harden it:** in the server's console, as root, run the hardening script pinned to a commit and checked by fingerprint (get both from the latest commit that changed `server/deploy/harden/harden.sh`):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/manjunathhari/jarvisdesktop/<sha>/server/deploy/harden/harden.sh -o /root/harden.sh \
     && echo "<sha256>  /root/harden.sh" | sha256sum -c && bash /root/harden.sh
   ```
   Approve the Tailscale link it prints. It creates fresh database passwords and the two signing keys in `/etc/jarvis/secrets/`, and asks (input hidden) for the Tailscale gateway auth key and the Anthropic API key (§8.1).
3. **Firewall at the provider:** on a server of its own, allow **only** UDP 41641 (Tailscale) — no 22, 80 or 443. (On the shared server, 22/80/443 stay open for pubmedgemini.)
4. **Deploy** as in §2, then `keys init` (§8.2). An empty JARVIS is a valid result; stop here if no data needs to move. A new server gets new signing keys, so every node enrols again (§8.4).
5. **Bring the data across (optional)** — §4 (ledger) and §7 (memory).
6. **Verify:** `SMOKE PASS` ×18 in the `init` logs, `memory ready` in the `core` logs, and the gateway checks (§8.3); run the **Server network check** workflow after updating the `SERVER_IP` repository secret and the expected port list in `.github/workflows/network-check.yml`.
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
| Recent logs | `docker logs --tail 50 jarvis-core-1` (also `-scheduler-1`, `-init-1`, `-backup-1`, `-gateway-1`, `-gateway-ts-1`, `-egress-proxy-1`) |
| Run the smoke checks again | `docker compose run --rm init smoke` (the P2 rows need init's gateway password) |
| Gateway up, on the tailnet only? | §8.3 |
| Nodes and keys | `docker exec jarvis-core-1 jarvis-core keys list` |
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
- **Rotate the signing keys:** §8.5. **Rotate the Anthropic key:** replace `anthropic_api_key` (root:10001, 0440), then `docker compose up -d --force-recreate core`.
- **Rotate the database passwords:** replace the files in `/etc/jarvis/secrets/` (keep root:10001, 0440), then `docker compose up -d` (init re-applies them to the roles). The Postgres superuser password lives in the data volume; change it with `ALTER ROLE postgres PASSWORD …` to match the new file.
- **Hard reset (data loss accepted):** `docker compose down -v` deletes `jarvis_pgdata`, `jarvis_backups`, `jarvis_memory`, `jarvis_memory_backups` and `jarvis_gateway_ts_state` (the sidecar then needs a fresh auth key, §8.1); the next `up` builds an empty JARVIS. **Before real memories exist, this is harmless; afterwards, copy a memory bundle off the server first.**

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

## 8. Gateway, nodes and keys (Phase 2)

Commands below run on the server as root (over `tailscale ssh`). `docker exec jarvis-core-1 jarvis-core …` runs an admin command as the app role inside the running core; it prints key IDs (public fingerprints), never a key.

### 8.1 Before the first Phase 2 deploy

1. **Tailscale ACL.** In the Tailscale admin, give the gateway its tag and let only Manj's own devices reach it, on 8443 only (the node keys then decide which of those are Jarvis nodes, §4.2). In the policy file, with your own login in place of the placeholder:
   ```jsonc
   "tagOwners": { "tag:jarvis-gateway": ["<your-tailscale-login>"] },
   "grants": [
     { "src": ["<your-tailscale-login>"], "dst": ["tag:jarvis-gateway"], "ip": ["tcp:8443"] }
   ]
   ```
   If the policy file still uses `acls`, the same rule is `{ "action": "accept", "src": ["<your-tailscale-login>"], "dst": ["tag:jarvis-gateway:8443"] }`. Nothing else may reach `tag:jarvis-gateway`.
2. **Auth key for the sidecar.** Settings → Keys → Generate auth key: **not reusable**, **tagged** `tag:jarvis-gateway`, short expiry. It is used once, at the sidecar's first login (`TS_AUTH_ONCE`); after that the device lives in volume `jarvis_gateway_ts_state`.
3. **Secrets.** Run the hardening script's secrets step (the same pinned-and-checked download as §3 step 2, then):
   ```bash
   bash /root/harden.sh --secrets-only
   ```
   It keeps every existing file, and creates `pg_gateway_password`, `core_signing_key`, `gateway_signing_key` (Ed25519, PEM) and `gateway_signing_key.pub` (raw 32 bytes). It asks, with input hidden, for `tailscale_gateway_authkey` (step 2) and `anthropic_api_key`; Enter skips one. It never prints a key. Everything ends up root:10001, 0440. A missing provided secret is reported as `missing …`: place it by hand (`umask 0337; cat > /etc/jarvis/secrets/<name>`, then `chown root:10001`) before deploying, or `docker compose up` fails.

### 8.2 First deploy: record the trusted keys (once)

Deploy as in §2. Then, once, record the core key and the gateway's public key as the ACTIVE trusted keys:
```bash
docker exec jarvis-core-1 jarvis-core keys init
docker exec jarvis-core-1 jarvis-core keys list     # CORE <id> ACTIVE, GATEWAY <id> ACTIVE
docker compose restart gateway                       # or wait for its next restart
```
`keys init` is idempotent, and refuses (`active_key_differs`) once a role has a different ACTIVE key, so after a rotation (§8.5) it is never needed again. Until it has run, the gateway refuses to start (no trusted keys) and Docker keeps restarting it; that is expected.

### 8.3 Gateway checks

| Check | Command | Expect |
|---|---|---|
| Bound to its tailnet address only | `docker logs jarvis-gateway-1 2>&1 \| grep -i 'running on'` | `http://100.x.y.z:8443`, never `0.0.0.0` |
| Sidecar on the tailnet | `docker exec jarvis-gateway-ts-1 tailscale --socket=/var/run/tailscale/tailscaled.sock status` | `jarvis-gw`, tagged, online |
| Gateway egress rules | `docker exec jarvis-gateway-ts-1 iptables -S JARVIS_GW_OUT` | ACCEPT on `lo` (127.0.0.0/8), on `tailscale0`, to the internal subnet; then DROP |
| Trusted keys loaded | `docker logs jarvis-gateway-1 2>&1 \| grep 'trusted keys'` | no ERROR lines (an ERROR names a missing secret, §8.5) |
| Proxy refuses other hosts | `docker logs jarvis-egress-proxy-1` | starts without config errors |

If `gateway-ts` restarts on its own, restart `gateway` after it (`docker compose restart gateway`): the gateway lives in the sidecar's network namespace.

### 8.4 Nodes: enrol, revoke, re-enrol

- **Enrol a device** (it must be one of Manj's devices on the tailnet, allowed by the ACL):
  ```bash
  docker exec -it jarvis-core-1 jarvis-core node enrol --person manj --kind DESKTOP --name "<device name>"
  ```
  It prints a one-time enrolment code, valid 15 minutes, to the terminal only (never to the log). Enter it on the device. The code also carries the key fingerprints the device pins.
- **List nodes:** `docker exec jarvis-postgres-1 psql -U postgres -d jarvis -c "SELECT id, name, kind, key_state, registered_at, revoked_at, last_seen_at FROM gw.node ORDER BY registered_at"`.
- **Revoke a node** (lost or retired device): `docker exec jarvis-core-1 jarvis-core node revoke <node_id>`. Its open connection is fenced at once and closed within the gateway's 60 s re-check; it can never connect again.
- **Re-enrol a STALE node** (it missed a key rotation, §8.5, and is refused with 4409): `docker exec -it jarvis-core-1 jarvis-core node reenrol <node_id>`. That revokes the old node and prints a new code for the same person, kind and name; enter it on the device.

### 8.5 Key rotation (core or gateway)

A key ID is the public key's fingerprint, `base32(sha256(public)[:10])`, lower case, no padding. A rotated private key lives in the secret `core_signing_key_<key_id>` (core) or `gateway_signing_key_<key_id>` (gateway); the original files keep serving only the bootstrap key. Because compose lists every secret, the new secret is added to `compose.yaml` and deployed **before** the key is announced: a key activates as soon as every node has acknowledged it (or 23 h after the announcement at the latest), and the process that signs with it must already hold it.

1. **Make the key and its ID** (here for core; for the gateway use `gateway_signing_key` and `--role GATEWAY`):
   ```bash
   cd /etc/jarvis/secrets
   (umask 0337; openssl genpkey -algorithm ed25519 -out core_signing_key.new)
   (umask 0337; openssl pkey -in core_signing_key.new -pubout -outform DER | tail -c 32 > core_signing_key.new.pub)
   KEY_ID=$(openssl dgst -sha256 -binary core_signing_key.new.pub | head -c 10 | base32 | tr -d '=' | tr 'A-Z' 'a-z')
   mv core_signing_key.new "core_signing_key_$KEY_ID"
   chown root:10001 "core_signing_key_$KEY_ID" core_signing_key.new.pub
   echo "$KEY_ID"
   ```
2. **Give it to the process:** in `server/deploy/compose.yaml`, add `core_signing_key_<KEY_ID>: { file: /etc/jarvis/secrets/core_signing_key_<KEY_ID> }` under `secrets:` and `- core_signing_key_<KEY_ID>` to the `core` service's secrets (for the gateway: the `gateway` service). Key IDs are public, so this is an ordinary commit. Deploy it (§2).
3. **Announce it** to every active node:
   ```bash
   docker exec -i jarvis-core-1 jarvis-core keys announce --role CORE --public-key-file /dev/stdin < core_signing_key.new.pub
   ```
   It prints the key ID (it must match `KEY_ID`) and the secret it expects. `keys list` shows it NEXT, with its activation deadline, and a `MISSING SECRET` line if core can't load it (fix that before the deadline). For a gateway key, the gateway logs an ERROR on each 60 s re-read while its secret is missing.
4. **After activation** (`keys list` shows the new key ACTIVE and the old one RETIRED `(overlap)`): run `keys list` again and re-enrol every `STALE` node it lists (§8.4); those devices were offline and missed the announcement.
5. **After the 24 h overlap:** `docker exec jarvis-core-1 jarvis-core keys retire <old_key_id>`. Nodes stop trusting the old key; the gateway closes connections that still use a replaced gateway key.
6. Keep the old secret files until the retire is done; then they can go (never delete the bootstrap `core_signing_key` / `gateway_signing_key` while their key is ACTIVE).

### 8.6 Live checks (after a deploy Manj has approved)

Scripted, fictional test phrases only (§5.7): what is said is stored on the shared server.

1. **Smoke:** the `init` logs show 18 `SMOKE PASS` lines (§2 step 3).
2. **Gateway path, end to end:** from Manj's PC on the tailnet, enrol a fresh test node with a new code (§8.4), then run the Python fake node once against `ws://<jarvis-gw tailnet address>:8443/v1/node` (plain WebSocket inside the encrypted tailnet): enrol, hello, one utterance with a fictional phrase, the reply chunks, ack. Revoke the test node afterwards. (The fake node in `server/tests/fakes/fake_node.py` drives an in-process test client today; this check needs a small runner that points it at the real gateway.)
3. **Latency (§6.2, target: the first spoken sentence within 3 s of the end of speech):** in the same run, time from sending `utterance` to the first `reply.chunk` received, over five fictional phrases, and note the worst. Core writes each speech piece when the next one arrives and keeps the last piece back to write as the final chunk after the call is settled (Task 10), so the first chunk lags Claude's first text by one streaming delta, and a reply that arrives as a single delta appears only when the call ends. If a turn misses 3 s, the `core` and `gateway` timing logs show where (§15).
4. **Gateway bound to the tailnet only:** §8.3, first row.
5. **P2-S5 (gateway egress blocked):** over Tailscale SSH on the server,
   ```bash
   docker compose exec gateway python -c "import socket; socket.create_connection(('1.1.1.1', 443), 3)"
   ```
   must fail (timeout or refused).
6. **P2-S7 (core reaches only Claude):** from `core`, any host other than `api.anthropic.com` must fail, directly and through the proxy, while a request to Claude through the proxy succeeds:
   ```bash
   docker compose exec core python -c "import socket; socket.create_connection(('1.1.1.1', 443), 3)"          # must fail
   docker compose exec core python -c "import httpx; httpx.get('https://example.com', proxy='http://egress-proxy:3128', timeout=5)"   # must fail (403 at CONNECT)
   docker compose exec core python -c "import httpx; print(httpx.get('https://api.anthropic.com/v1/models', proxy='http://egress-proxy:3128', timeout=10).status_code)"   # prints 401 (reached Claude; no key sent)
   ```
7. **P0-8:** the external scan (**Server network check** workflow) still shows only 22/80/443 on the shared server.
