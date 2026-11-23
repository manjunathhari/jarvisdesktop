"""The gateway process (spec §4, §6.1, §7): node WebSockets over Tailscale only.

`app` builds the FastAPI app and the `jarvis-gateway` entry point; `session` runs one node
connection (handshake, inbound recording, revocation re-check); `tailscale` asks the sidecar's
local API for peer identities and the tailnet address; `keys` holds the signing material.
"""
