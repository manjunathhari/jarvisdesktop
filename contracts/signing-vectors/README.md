# Signing vectors

Golden test vectors for the JARVIS signing rules (spec §7.1). The Python server
(`server/tests/test_signing.py`, `server/tests/test_node_rules.py`) and the Rust node
both check their code against these committed files. **Neither ever regenerates them.**

## The rules being tested

- Signed bytes: `"jarvis-sig-v1\n" + purpose + "\n" + key_id + "\n" + JCS(object)`, where
  JCS is RFC 8785 and `purpose` is `node-msg`, `gw-transport` or `core-authz`.
- Signature: Ed25519 over those bytes, base64url without padding (86 characters).
- An envelope's `sig` covers every other envelope field.
- Envelopes carry no `key_id` field. A node signs `node-msg` envelopes under `key_id =
  fingerprint(node public key)` (16 characters); the gateway signs `gw-transport` envelopes under the
  `key_id` its connection's `challenge` announced (spec §7.1, R12-6). The vectors' `node-1` is only a
  placeholder `key_id` for testing the byte layout.

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

The node-side rules (spec §6.1, §7.1, §7.2). Every case has `name`, `check` (which reference
rule it exercises), its inputs, `expect` (`accept` or `reject`) and `reason` (null for
`accept`, otherwise the first rule that fails, in the order listed). Keys in `trusted_keys`
and `pinned_gateway` are raw 32-byte public keys in hex; in messages they are base64url.

| `check` | Inputs | Reasons, in order |
|---|---|---|
| `node_action` | `trusted_keys` (CORE keys only), `receiving_node_id`, `now`, `authz` | `malformed` (including a time without an offset), `untrusted_key_id`, `bad_signature`, `wrong_kind`, `wrong_target`, `expired` (`now` > `valid_until` + 30 s) |
| `keyset` | `trusted_keys`, `now`, `payload` (the keyset payload without its `authz`), `authz` | `malformed`, `untrusted_key_id`, `bad_signature`, `wrong_kind`, `expired` (from the signed `authz.object.valid_until`, + 30 s), `keyset_mismatch` (payload `role`, `key_id` or `public_key` differs from `authz.object.args`) |
| `challenge` | `envelope` (the full signed envelope), `pinned_gateway` (GATEWAY keys, or null while enrolling), `enrol_fingerprint` (the code's gateway fingerprint, or null) | `malformed`, `malformed_key`, `unpinned_key`, `fingerprint_mismatch`, `bad_signature` (`gw-transport`) |
| `enrolment_welcome` | `welcome` (payload), `core_fingerprint` (from the code) | `malformed`, `enrolment_missing`, `malformed_key`, `core_fingerprint_mismatch` |
| `seq` | `seqs`: a fresh checker is fed each value in order | `bad_seq` on the last value; every earlier one passes |

Base64url keys and signatures are decoded strictly: no padding, URL-safe alphabet only, and a final character with non-zero unused trailing bits is rejected (`malformed_key` for a key), never normalised (case `reject-non-canonical-base64url-key`).

A fingerprint is the first 10 bytes of SHA-256 of the raw public key, lower-case base32
without padding (16 characters); the generator has its own RFC 4648 base32 encoder.

Rule 1 (the `gw-transport` envelope of a `node.action`) and rule 5 (an unused
`execution_key` for an IRREVERSIBLE action) are not covered here: they need the connection
and the node's journal.
