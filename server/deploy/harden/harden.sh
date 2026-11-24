#!/usr/bin/env bash
# JARVIS server hardening (ADR-009). Run as root on Ubuntu 24.04, e.g. from the Hostinger web
# console. Safe to run again: every step checks before it changes anything.
#
# What it does
#   1. Automatic security updates (unattended-upgrades)
#   2. fail2ban on SSH
#   3. SSH: keys only, no passwords; root by key only (pubmedgemini's deploy key keeps working)
#   4. Tailscale with Tailscale SSH (Manj logs in over the tailnet, no keys to manage)
#   5. /etc/jarvis/secrets (root:10001, every file 0440):
#      - four generated database passwords (superuser, owner, app, gateway);
#      - two generated Ed25519 signing keys, core_signing_key and gateway_signing_key (PEM),
#        and gateway_signing_key.pub (the raw 32-byte public key, for `jarvis-core keys init`);
#      - two provided secrets, asked for (hidden) when missing: tailscale_gateway_authkey and
#        anthropic_api_key.
#      Existing files are kept, never regenerated. No key or password is ever printed.
#
#   bash harden.sh --secrets-only   runs step 5 alone (e.g. to add the Phase 2 secrets).
#
# What it does NOT touch: pubmedgemini, Traefik, Ollama, the existing Postgres 16, Docker.
set -euo pipefail

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

SECRETS_DIR="${JARVIS_HARDEN_SECRETS_DIR:-/etc/jarvis/secrets}"
ED25519_SPKI_PREFIX=302a300506032b6570032100   # DER SubjectPublicKeyInfo of an Ed25519 key

seal() { chown root:10001 "$1"; chmod 0440 "$1"; }

secrets_step() {
  local name f value spki
  getent group 10001 >/dev/null || groupadd --system --gid 10001 jarvis-secrets
  install -d -m 0750 -o root -g 10001 "${SECRETS_DIR%/*}" "$SECRETS_DIR"

  for name in pg_superuser_password pg_owner_password pg_app_password pg_gateway_password; do
    f=$SECRETS_DIR/$name
    if [[ ! -s $f ]]; then
      (umask 0337; openssl rand -base64 33 | tr -d '/+=\n' > "$f")
      echo "created $name"
    else
      echo "kept existing $name"
    fi
    seal "$f"
  done

  for name in core_signing_key gateway_signing_key; do
    f=$SECRETS_DIR/$name
    if [[ ! -s $f ]]; then
      (umask 0337; openssl genpkey -algorithm ed25519 -out "$f")
      echo "created $name (Ed25519)"
    else
      echo "kept existing $name"
    fi
    spki=$(openssl pkey -in "$f" -pubout -outform DER | od -An -v -tx1 | tr -d ' \n')
    if [[ ${#spki} -ne 88 || ${spki:0:24} != "$ED25519_SPKI_PREFIX" ]]; then
      echo "$f is not an Ed25519 private key; move it away and run again." >&2
      exit 1
    fi
    seal "$f"
  done
  # The raw public key is the last 32 bytes of the DER public key.
  f=$SECRETS_DIR/gateway_signing_key.pub
  (umask 0337; rm -f "$f.tmp"
   openssl pkey -in "$SECRETS_DIR/gateway_signing_key" -pubout -outform DER | tail -c 32 > "$f.tmp")
  [[ $(stat -c %s "$f.tmp") -eq 32 ]] || { echo "could not write $f" >&2; exit 1; }
  mv -f "$f.tmp" "$f"
  seal "$f"
  echo "wrote gateway_signing_key.pub"

  for name in tailscale_gateway_authkey anthropic_api_key; do
    f=$SECRETS_DIR/$name
    if [[ ! -s $f && -t 0 ]]; then
      case $name in
        tailscale_gateway_authkey) echo "Tailscale admin > Settings > Keys: a tagged (tag:jarvis-gateway), non-reusable auth key." ;;
        anthropic_api_key) echo "The Anthropic API key for Jarvis's brain." ;;
      esac
      read -rsp "Paste $name (input hidden; Enter to skip): " value
      echo
      if [[ -n $value ]]; then
        (umask 0337; printf '%s' "$value" > "$f")
      fi
      value=
    fi
    if [[ -s $f ]]; then
      seal "$f"
      echo "have $name"
    else
      echo "missing $name: place it in $f (root:10001, 0440) before deploying"
    fi
  done
}

if [[ ${1:-} == --secrets-only ]]; then
  command -v openssl >/dev/null || { echo "openssl is needed." >&2; exit 1; }
  say "Secrets only"
  secrets_step
  exit 0
fi
grep -q 'VERSION_ID="24.04"' /etc/os-release || { echo "Expected Ubuntu 24.04." >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive

say "1/5 Automatic security updates"
apt-get update -q
apt-get install -y -q unattended-upgrades fail2ban openssl curl
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades >/dev/null

say "2/5 fail2ban on SSH"
cat > /etc/fail2ban/jail.d/jarvis-sshd.local <<'EOF'
[sshd]
enabled  = true
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
systemctl enable --now fail2ban >/dev/null
systemctl restart fail2ban

say "3/5 SSH: keys only"
if [[ ! -s /root/.ssh/authorized_keys ]]; then
  echo "No SSH keys for root: not turning off passwords (nothing would be able to log in)." >&2
else
  # 00- sorts first, and sshd keeps the first value it reads, so this wins over cloud-init's file.
  cat > /etc/ssh/sshd_config.d/00-jarvis-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
  sshd -t
  systemctl reload ssh
  echo "Password login off. Keys still allowed: $(wc -l < /root/.ssh/authorized_keys)."
fi

say "4/5 Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if tailscale status >/dev/null 2>&1; then
  echo "Already on the tailnet as: $(tailscale status --self --peers=false | awk 'NR==1{print $2}')"
  tailscale set --ssh
else
  echo ">>> Open the link below on your PC and approve this server. <<<"
  tailscale up --ssh --hostname=jarvis-server
fi

say "5/5 Secrets folder"
secrets_step

say "Done"
echo "Tailscale address: $(tailscale ip -4 2>/dev/null || echo 'not connected yet')"
echo "Next: tell Claude it finished. The firewall is changed separately, through the Hostinger connector."
echo "After the first deploy: docker exec jarvis-core-1 jarvis-core keys init (runbook §8)."
