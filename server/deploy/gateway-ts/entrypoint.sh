#!/bin/sh
# gateway-ts entrypoint (spec §4.2, K2-4). The gateway runs in this container's network
# namespace (network_mode: service:gateway-ts), so these OUTPUT rules apply to it. Traffic from
# the gateway's UID may leave only on tailscale0 (replies to nodes), to the jarvis_internal
# subnet (Postgres), or to loopback (Docker's embedded DNS at 127.0.0.11, which the gateway
# needs to find postgres). Everything else from that UID is dropped. tailscaled runs as root
# and is not affected. Then the image's own entrypoint, containerboot, starts tailscaled.
#
# compose.yaml carries an inline copy of this file (configs: gateway_ts_entrypoint, with $
# written as $$); tests/test_compose.py checks the two match. P2-S5 checks it live.
set -eu

GW_UID="${JARVIS_GATEWAY_UID:-10001}"
CHAIN=JARVIS_GW_OUT
fail() { echo "gateway-ts: $*; refusing to start" >&2; exit 1; }

# jarvis_internal is the network that holds postgres: its interface and subnet.
pg_ip="$(getent hosts postgres | awk '{ print $1; exit }')"
[ -n "$pg_ip" ] || fail "cannot resolve postgres"
dev="$(ip -4 route get "$pg_ip" | awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }')"
[ -n "$dev" ] || fail "no route to postgres"
subnet="$(ip -4 route show dev "$dev" scope link | awk '{ print $1; exit }')"
[ -n "$subnet" ] || fail "no subnet on $dev"

iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
iptables -A "$CHAIN" -o lo -d 127.0.0.0/8 -j ACCEPT
iptables -A "$CHAIN" -o tailscale0 -j ACCEPT
iptables -A "$CHAIN" -o "$dev" -d "$subnet" -j ACCEPT
iptables -A "$CHAIN" -j DROP
iptables -C OUTPUT -m owner --uid-owner "$GW_UID" -j "$CHAIN" 2>/dev/null \
  || iptables -I OUTPUT 1 -m owner --uid-owner "$GW_UID" -j "$CHAIN"

# IPv6, when the namespace has it: only loopback and tailscale0.
if [ -s /proc/net/if_inet6 ]; then
  ip6tables -N "$CHAIN" 2>/dev/null || ip6tables -F "$CHAIN"
  ip6tables -A "$CHAIN" -o lo -d ::1/128 -j ACCEPT
  ip6tables -A "$CHAIN" -o tailscale0 -j ACCEPT
  ip6tables -A "$CHAIN" -j DROP
  ip6tables -C OUTPUT -m owner --uid-owner "$GW_UID" -j "$CHAIN" 2>/dev/null \
    || ip6tables -I OUTPUT 1 -m owner --uid-owner "$GW_UID" -j "$CHAIN"
fi
echo "gateway-ts: egress rules for UID $GW_UID in place (internal $subnet on $dev)"

# The auth key is read from its secret file here, so it is never in the container's config.
# It is used only for the first login (TS_AUTH_ONCE); after that the state volume holds it.
TS_AUTHKEY="$(cat /run/secrets/tailscale_gateway_authkey)"
export TS_AUTHKEY
exec /usr/local/bin/containerboot
