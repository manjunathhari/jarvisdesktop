"""JARVIS's self-improvement team: tickets, the pre-push check, the roles and their stages.

The orchestrator in this package holds the keys and never runs an agent tool. The agents run in
the sandbox (see sandbox_runner), which holds no secrets and hands back a patch.
"""
