# Signing vectors

Golden test vectors for the JARVIS signing rules (spec §7.1). The Python server
(`server/tests/test_signing.py`, `server/tests/test_node_rules.py`) and the Rust node
both check their code against these committed files. **Neither ever regenerates them.**

## The rules being tested

- Signed bytes: `"jarvis-sig-v1\n" + purpose + "\n" + key_id + "\n" + JCS(object)`, where
  JCS is RFC 8785 and `purpose` is `node-msg`, `gw-transport` or `core-authz`.
- Signature: Ed25519 over those bytes, base64url without padding (86 characters).
- An envelope's `sig` covers every other envelope field.

## How they were made

`generate.mjs` is a separate implementation that imports nothing from this repository:

- Node.js 22's built-in `crypto` (`crypto.sign(null, bytes, ed25519Key)`);
- the npm package [`canonicalize`](https://github.com/erdtman/canonicalize), the JCS
  implementation by one of RFC 8785's authors, pinned to an exact version in
  `package-lock.json`.

Everything is fixed (seeds, objects, times), so a re-run is byte-identical. CI runs
`npm ci` and the generator, and fails if either file differs from the committed one:

```sh
npm ci
node generate.mjs vectors     > /tmp/v.json && diff vectors.json /tmp/v.json
node generate.mjs authz-cases > /tmp/a.json && diff authz-cases.json /tmp/a.json
```

**Keys.** Every key is a fixed *test* seed: SHA-256 of the public label
`jarvis-signing-vectors test seed: <label>` (labels `core-1`, `core-2`, `gw-1`, `node-1`,
`unknown-1`). They are not secrets and must never be used outside tests.

**The `rfc8785-appendix` case** uses the sample published in RFC 8785: the input of §3.2.2,
the canonical output of §3.2.3, and that output's UTF-8 bytes in hex from §3.2.4, copied
from the RFC text at rfc-editor.org (`rfc8785.txt`). The generator refuses to run unless
`canonicalize` reproduces the §3.2.4 bytes exactly, and the Python test checks the same
bytes independently. This case has non-integer numbers; JARVIS's own signed objects
use integers only.

## `vectors.json`

A list of `{name, purpose, key_id, seed_hex, object, expected_canonical_utf8,
expected_signed_bytes_hex, expected_sig}`. `seed_hex` is the 32-byte Ed25519 private seed.
Pairs named `same-object-two-purposes:*` and `same-object-two-key-ids:*` sign one object with
one key under different bindings, so their signatures must differ.

## `authz-cases.json`

The node rules (spec §7.1), as `{name, trusted_keys, receiving_node_id, now, authz, expect,
reason}`, plus `payload` on `keyset` cases:

- `trusted_keys` maps a trusted core `key_id` to its raw 32-byte public key, in hex;
- `expect` is `accept` or `reject`;
- `reason` is null for `accept`, otherwise the first rule that fails.

**`node.action` cases** (no `payload`; rules 2 to 4) are checked in this order:
`malformed` (including a time without a UTC offset), `untrusted_key_id`, `bad_signature`,
`wrong_kind`, `wrong_target`, `expired` (`now` > `valid_until` + 30 s).

**`keyset` cases** carry `payload`: the `keyset` message payload (`role`, `key_id`,
`public_key`) without its `authz`, which is the case's `authz`. They are checked in this order:
`malformed`, `untrusted_key_id`, `bad_signature`, `wrong_kind`, `keyset_mismatch` (the
payload's `role`, `key_id` or `public_key` differs from the signed `authz.object.args`).

Rule 1 (the `gw-transport` envelope) and rule 5 (an unused `execution_key` for an
IRREVERSIBLE action) are not covered here: they need the connection and the node's journal.
