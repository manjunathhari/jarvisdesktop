#!/bin/sh
# gateway-ts entrypoint (spec §4.2, K2-4, R12-33). The gateway runs in this container's network
# namespace (network_mode: service:gateway-ts), so these OUTPUT rules apply to it. The gateway
# uses no DNS: it reaches Postgres at a fixed address (JARVIS_POSTGRES_IP). Traffic from the
# gateway's UID (10001) may only
#   - open TCP connections to the Postgres address on port 5432, and
#   - send established or related packets on tailscale0 (replies to nodes).
# Everything else from that UID is dropped: no loopback (so no Docker DNS at 127.0.0.11), no
# other jarvis_internal host or port (so no egress proxy), and no new connection on tailscale0
# (so no quad-100 DNS and no other tailnet device). IPv6 gets the same rules (it has no
# Postgres address), and the sidecar refuses to start if IPv6 is on and they can't be
# installed. The rules hold for every socket not owned by root, not only UID 10001, so a
# gateway run under any other UID is closed too (default closed, final review S-2). Packets
# with no owning socket (kernel replies, closing sockets) are not matched, as before: an
# inverted UID match alone would catch them and could break tailscaled's own connections.
# tailscaled runs as root and is not affected. Then the image's own entrypoint, containerboot,
# starts tailscaled.
#
# compose.yaml carries an inline copy of this file (x-gateway-ts-entrypoint, with $ written as
# $$), passed in as JARVIS_TS_ENTRYPOINT and written to /tmp at start, because the container is
# read-only and Compose can't place an inline config file in it. tests/test_compose.py checks the
# two match. P2-S5 checks it live.
set -eu

PG_IP="${JARVIS_POSTGRES_IP:-}"
CHAIN=JARVIS_GW_OUT
fail() { echo "gateway-ts: $*; refusing to start" >&2; exit 1; }

case "$PG_IP" in
  "" | *[!0-9.]* ) fail "JARVIS_POSTGRES_IP is not an IPv4 address" ;;
esac

# Use the iptables backend this kernel offers: nftables first (Ubuntu 24.04 hosts have no legacy
# tables, and the image's plain `iptables` is the legacy one), then legacy. The choice is written
# to /tmp/jarvis-iptables for the health check; a backend that can't list OUTPUT is never used.
pick() {
  for b in "$1-nft" "$1-legacy" "$1"; do
    if command -v "$b" >/dev/null 2>&1 && "$b" -t filter -S OUTPUT >/dev/null 2>&1; then
      echo "$b"
      return 0
    fi
  done
  return 1
}
IPT="$(pick iptables)" || fail "no working iptables backend (nft or legacy)"
IP6T="$(pick ip6tables)" || IP6T=""

"$IPT" -N "$CHAIN" 2>/dev/null || "$IPT" -F "$CHAIN"
"$IPT" -A "$CHAIN" -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
"$IPT" -A "$CHAIN" -d "$PG_IP" -p tcp --dport 5432 -j ACCEPT
"$IPT" -A "$CHAIN" -j DROP
"$IPT" -C OUTPUT -m owner --socket-exists ! --uid-owner 0 -j "$CHAIN" 2>/dev/null \
  || "$IPT" -I OUTPUT 1 -m owner --socket-exists ! --uid-owner 0 -j "$CHAIN"

ipv6_on() {
  [ -e /proc/net/if_inet6 ] && [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || echo 1)" = 0 ]
}
if [ -n "$IP6T" ]; then
  "$IP6T" -N "$CHAIN" 2>/dev/null || "$IP6T" -F "$CHAIN"
  "$IP6T" -A "$CHAIN" -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  "$IP6T" -A "$CHAIN" -j DROP
  "$IP6T" -C OUTPUT -m owner --socket-exists ! --uid-owner 0 -j "$CHAIN" 2>/dev/null \
    || "$IP6T" -I OUTPUT 1 -m owner --socket-exists ! --uid-owner 0 -j "$CHAIN"
  v6="and IPv6"
elif ipv6_on; then
  fail "IPv6 is on but ip6tables does not work"
else
  v6="(IPv6 off)"
fi
printf '%s %s\n' "$IPT" "$IP6T" > /tmp/jarvis-iptables
echo "gateway-ts: egress rules for every non-root UID in place ($IPT): Postgres $PG_IP:5432, tailscale0 replies; IPv4 $v6"

# The auth key is read from its secret file here, so it is never in the container's config.
# It is used only for the first login (TS_AUTH_ONCE); after that the state volume holds it.
TS_AUTHKEY="$(cat /run/secrets/tailscale_gateway_authkey)"
export TS_AUTHKEY
exec /usr/local/bin/containerboot
