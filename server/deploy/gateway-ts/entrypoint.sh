#!/bin/sh
# gateway-ts entrypoint (spec §4.2, K2-4, R12-33). The gateway runs in this container's network
# namespace (network_mode: service:gateway-ts), so these OUTPUT rules apply to it. The gateway
# uses no DNS: it reaches Postgres at a fixed address (JARVIS_POSTGRES_IP). Traffic from the
# gateway's UID may only
#   - open TCP connections to the Postgres address on port 5432, and
#   - send established or related packets on tailscale0 (replies to nodes).
# Everything else from that UID is dropped: no loopback (so no Docker DNS at 127.0.0.11), no
# other jarvis_internal host or port (so no egress proxy), and no new connection on tailscale0
# (so no quad-100 DNS and no other tailnet device). IPv6 gets the same rules (it has no
# Postgres address), and the sidecar refuses to start if IPv6 is on and they can't be
# installed. tailscaled runs as root and is not affected. Then the image's own entrypoint,
# containerboot, starts tailscaled.
#
# compose.yaml carries an inline copy of this file (configs: gateway_ts_entrypoint, with $
# written as $$); tests/test_compose.py checks the two match. P2-S5 checks it live.
set -eu

GW_UID="${JARVIS_GATEWAY_UID:-10001}"
PG_IP="${JARVIS_POSTGRES_IP:-}"
CHAIN=JARVIS_GW_OUT
fail() { echo "gateway-ts: $*; refusing to start" >&2; exit 1; }

case "$PG_IP" in
  "" | *[!0-9.]* ) fail "JARVIS_POSTGRES_IP is not an IPv4 address" ;;
esac

iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
iptables -A "$CHAIN" -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A "$CHAIN" -d "$PG_IP" -p tcp --dport 5432 -j ACCEPT
iptables -A "$CHAIN" -j DROP
iptables -C OUTPUT -m owner --uid-owner "$GW_UID" -j "$CHAIN" 2>/dev/null \
  || iptables -I OUTPUT 1 -m owner --uid-owner "$GW_UID" -j "$CHAIN"

ipv6_on() {
  [ -e /proc/net/if_inet6 ] && [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || echo 1)" = 0 ]
}
if ip6tables -S OUTPUT >/dev/null 2>&1; then
  ip6tables -N "$CHAIN" 2>/dev/null || ip6tables -F "$CHAIN"
  ip6tables -A "$CHAIN" -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  ip6tables -A "$CHAIN" -j DROP
  ip6tables -C OUTPUT -m owner --uid-owner "$GW_UID" -j "$CHAIN" 2>/dev/null \
    || ip6tables -I OUTPUT 1 -m owner --uid-owner "$GW_UID" -j "$CHAIN"
  v6="and IPv6"
elif ipv6_on; then
  fail "IPv6 is on but ip6tables does not work"
else
  v6="(IPv6 off)"
fi
echo "gateway-ts: egress rules for UID $GW_UID in place: Postgres $PG_IP:5432, tailscale0 replies; IPv4 $v6"

# The auth key is read from its secret file here, so it is never in the container's config.
# It is used only for the first login (TS_AUTH_ONCE); after that the state volume holds it.
TS_AUTHKEY="$(cat /run/secrets/tailscale_gateway_authkey)"
export TS_AUTHKEY
exec /usr/local/bin/containerboot
