// Independent generator of the JARVIS golden signing vectors.
//
// It imports nothing from this repository: only Node's built-in crypto and the
// pinned npm package `canonicalize` (RFC 8785 JCS, by one of the RFC's authors).
// Everything is fixed (seeds, objects, times), so a re-run gives byte-identical output.
//
// Usage:
//   node generate.mjs vectors       > vectors.json
//   node generate.mjs authz-cases   > authz-cases.json
//
// Signed bytes (spec §7.1):
//   "jarvis-sig-v1\n" + purpose + "\n" + key_id + "\n" + JCS(object)
// Signature: Ed25519 over those bytes, base64url without padding.

import { createHash, createPrivateKey, createPublicKey, sign } from "node:crypto";
import canonicalize from "canonicalize";

// ---------------------------------------------------------------- keys

// Test seeds only: SHA-256 of a fixed, public label. Never used outside tests.
const seedFor = (label) =>
  createHash("sha256").update(`jarvis-signing-vectors test seed: ${label}`, "utf8").digest();

// PKCS#8 DER prefix for a raw 32-byte Ed25519 seed (RFC 8410).
const PKCS8_ED25519_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

const keyFor = (label) => {
  const seed = seedFor(label);
  const priv = createPrivateKey({
    key: Buffer.concat([PKCS8_ED25519_PREFIX, seed]),
    format: "der",
    type: "pkcs8",
  });
  const pubRaw = Buffer.from(createPublicKey(priv).export({ format: "jwk" }).x, "base64url");
  return { seed, priv, pubRaw };
};

const KEYS = {
  "core-1": keyFor("core-1"),
  "core-2": keyFor("core-2"),
  "gw-1": keyFor("gw-1"),
  "gw-2": keyFor("gw-2"),
  "node-1": keyFor("node-1"),
  "unknown-1": keyFor("unknown-1"),
};

// ---------------------------------------------------------------- signing

const PREFIX = "jarvis-sig-v1\n";

const jcs = (obj) => {
  const out = canonicalize(obj);
  if (typeof out !== "string") throw new Error("canonicalize returned no string");
  return out;
};

const signedBytes = (purpose, keyId, obj) =>
  Buffer.concat([Buffer.from(`${PREFIX}${purpose}\n${keyId}\n`, "utf8"), Buffer.from(jcs(obj), "utf8")]);

const signWith = (keyLabel, purpose, keyId, obj) =>
  sign(null, signedBytes(purpose, keyId, obj), KEYS[keyLabel].priv).toString("base64url");

// ---------------------------------------------------------------- RFC 8785 sample

// RFC 8785 §3.2.2 (input), §3.2.3 (canonical output) and §3.2.4 (its UTF-8 bytes in hex),
// copied from the RFC text at rfc-editor.org. The generator refuses to run if
// `canonicalize` does not reproduce the RFC's bytes exactly.
const RFC8785_INPUT = String.raw`{
  "numbers": [333333333.33333329, 1E30, 4.50,
              2e-3, 0.000000000000000000000000001],
  "string": "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"\/",
  "literals": [null, true, false]
}`;
const RFC8785_OUTPUT_HEX = [
  "7b 22 6c 69 74 65 72 61 6c 73 22 3a 5b 6e 75 6c 6c 2c 74 72",
  "75 65 2c 66 61 6c 73 65 5d 2c 22 6e 75 6d 62 65 72 73 22 3a",
  "5b 33 33 33 33 33 33 33 33 33 2e 33 33 33 33 33 33 33 2c 31",
  "65 2b 33 30 2c 34 2e 35 2c 30 2e 30 30 32 2c 31 65 2d 32 37",
  "5d 2c 22 73 74 72 69 6e 67 22 3a 22 e2 82 ac 24 5c 75 30 30",
  "30 66 5c 6e 41 27 42 5c 22 5c 5c 5c 5c 5c 22 2f 22 7d",
]
  .join(" ")
  .replaceAll(" ", "");

const rfcObject = JSON.parse(RFC8785_INPUT);
if (Buffer.from(jcs(rfcObject), "utf8").toString("hex") !== RFC8785_OUTPUT_HEX) {
  throw new Error("canonicalize does not reproduce RFC 8785 §3.2.3/§3.2.4");
}

// ---------------------------------------------------------------- vectors

const vector = (name, purpose, keyLabel, keyId, object) => ({
  name,
  purpose,
  key_id: keyId,
  seed_hex: KEYS[keyLabel].seed.toString("hex"),
  object,
  expected_canonical_utf8: jcs(object),
  expected_signed_bytes_hex: signedBytes(purpose, keyId, object).toString("hex"),
  expected_sig: signWith(keyLabel, purpose, keyId, object),
});

const SHARED = { kind: "ping", note: "same object, different binding", seq: 7 };

const vectors = () => [
  vector("rfc8785-appendix", "core-authz", "core-1", "core-1", rfcObject),
  vector("ascii-simple", "node-msg", "node-1", "node-1", { kind: "ping", text: "hello jarvis" }),
  vector("nested-keys-unsorted", "gw-transport", "gw-1", "gw-1", {
    zeta: 1,
    alpha: { yankee: [{ delta: 1, charlie: 2 }], bravo: "b" },
    mike: null,
  }),
  vector("kannada-and-emoji", "node-msg", "node-1", "node-1", {
    screen_summary: null,
    text: "ನಮಸ್ಕಾರ 🙏",
  }),
  vector("hindi", "node-msg", "node-1", "node-1", {
    screen_summary: "समाचार",
    text: "नमस्ते, आज का मौसम कैसा है?",
  }),
  vector("integers-only", "core-authz", "core-1", "core-1", {
    amount_paise: 500000,
    attempts: 0,
    cap_paise: 400000,
    last_seq: 2147483647,
    seq: 1,
  }),
  vector("same-object-two-purposes:node-msg", "node-msg", "core-1", "core-1", SHARED),
  vector("same-object-two-purposes:core-authz", "core-authz", "core-1", "core-1", SHARED),
  vector("same-object-two-key-ids:core-1", "core-authz", "core-1", "core-1", SHARED),
  vector("same-object-two-key-ids:core-2", "core-authz", "core-1", "core-2", SHARED),
  vector("envelope-utterance", "node-msg", "node-1", "node-1", {
    version: 1,
    message_id: "0b8f7c1e-3a8e-4d4f-9a55-2f6f1c3d9e01",
    node_id: "7a1d3c5e-0f2b-4c6d-8e9f-a0b1c2d3e4f5",
    session_id: null,
    seq: 3,
    sent_at: "2026-11-22T10:00:00Z",
    kind: "utterance",
    payload: { text: "ನಮಸ್ಕಾರ 🙏", screen_summary: null },
  }),
];

// ---------------------------------------------------------------- authz cases

const ME = "7a1d3c5e-0f2b-4c6d-8e9f-a0b1c2d3e4f5";
const OTHER_NODE = "9c8b7a6f-5e4d-4c3b-8a29-1f0e0d0c0b0a";
const VALID_UNTIL = "2026-11-22T10:02:00Z";

const actionObject = (overrides = {}) => ({
  v: 1,
  kind: "node.action",
  action_kind: "news.show",
  args: { channel: "headlines" },
  target_node_id: ME,
  person_id: "3e2d1c0b-9a8f-4e7d-8c6b-5a4f3e2d1c0b",
  approval_id: null,
  execution_key: null,
  reply_id: "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
  summary: null,
  issued_at: "2026-11-22T10:00:00Z",
  valid_until: VALID_UNTIL,
  ...overrides,
});

const authzOf = (object, { keyLabel = "core-1", keyId = "core-1", purpose = "core-authz" } = {}) => ({
  object,
  key_id: keyId,
  sig: signWith(keyLabel, purpose, keyId, object),
});

const TRUSTED = { "core-1": KEYS["core-1"].pubRaw.toString("hex") };

// Every case names the reference check it exercises (`check`), then its inputs.
const authzCase = (name, now, authz, expect, reason) => ({
  name,
  check: "node_action",
  trusted_keys: TRUSTED,
  receiving_node_id: ME,
  now,
  authz,
  expect,
  reason,
});

const keysetObject = (args, overrides = {}) => ({
  v: 1,
  kind: "keyset",
  action_kind: "keyset.add",
  args,
  target_node_id: null,
  person_id: null,
  approval_id: null,
  execution_key: null,
  reply_id: null,
  summary: null,
  issued_at: "2026-11-22T10:00:00Z",
  valid_until: "2026-11-23T10:00:00Z",
  ...overrides,
});

// A keyset announcement: the new key is in the payload and, signed, in authz.object.args.
// `payload` is the keyset message payload without its `authz` (which is the case's `authz`).
const NEW_CORE_KEY = {
  role: "core",
  key_id: "core-2",
  public_key: KEYS["core-2"].pubRaw.toString("base64url"),
};

// A gateway key announcement (R6-3): accepted, but its key is pinned as a GATEWAY key and
// never enters core trust.
const NEW_GATEWAY_KEY = {
  role: "gateway",
  key_id: "gw-2",
  public_key: KEYS["gw-2"].pubRaw.toString("base64url"),
};

// `receiving_node_id` is the node checking it (the target rule, R12-27); an accepted
// keyset also says which pin set its key goes to (`pins_as`, the role rule, R6-3).
const keysetCase = (name, now, payload, expect, reason, overrides = {}, args = NEW_CORE_KEY) => ({
  name,
  check: "keyset",
  trusted_keys: TRUSTED,
  receiving_node_id: ME,
  now,
  payload,
  authz: authzOf(keysetObject(args, overrides)),
  expect,
  reason,
  ...(expect === "accept" ? { pins_as: args.role } : {}),
});

const tampered = () => {
  const a = authzOf(actionObject());
  return { ...a, object: { ...a.object, args: { channel: "everything" } } };
};

// ---------------------------------------------------------------- fingerprints (RFC 4648 base32)

const B32 = "abcdefghijklmnopqrstuvwxyz234567";
const base32Lower = (bytes) => {
  let bits = 0;
  let value = 0;
  let out = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      out += B32[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += B32[(value << (5 - bits)) & 31];
  return out;
};
// First 10 bytes of SHA-256, lower-case base32 without padding: 16 characters.
const fingerprintOf = (pubRaw) =>
  base32Lower(createHash("sha256").update(pubRaw).digest().subarray(0, 10));

const b64 = (label) => KEYS[label].pubRaw.toString("base64url");
const hexKey = (label) => KEYS[label].pubRaw.toString("hex");

// ---------------------------------------------------------------- challenge cases

const NIL = "00000000-0000-0000-0000-000000000000";
const NONCE = createHash("sha256")
  .update("jarvis-signing-vectors test nonce", "utf8")
  .digest()
  .toString("base64url");

const challengeEnvelope = (
  { keyLabel = "gw-1", keyId = "gw-1", claimedLabel = keyLabel, issuedAt = "2026-11-22T10:00:00Z" } = {},
) => {
  const body = {
    version: 1,
    message_id: "8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5f60",
    node_id: NIL,
    session_id: null,
    seq: 1,
    sent_at: "2026-11-22T10:00:00Z",
    kind: "challenge",
    payload: {
      nonce: NONCE,
      issued_at: issuedAt,
      expires_at: "2026-11-22T10:00:30Z",
      key_id: keyId,
      public_key: b64(claimedLabel),
    },
  };
  return { ...body, sig: signWith(keyLabel, "gw-transport", keyId, body) };
};

const challengeCase = (name, envelope, pinnedGateway, enrolFingerprint, expect, reason) => ({
  name,
  check: "challenge",
  envelope,
  pinned_gateway: pinnedGateway,
  enrol_fingerprint: enrolFingerprint,
  expect,
  reason,
});

const PINNED_GW = { "gw-1": hexKey("gw-1") };

const tamperedChallenge = () => {
  const env = challengeEnvelope();
  return { ...env, payload: { ...env.payload, nonce: NONCE.replace(/^./, (c) => (c === "A" ? "B" : "A")) } };
};

// ---------------------------------------------------------------- enrolment-welcome cases

const welcomeWith = (enrolment) => ({
  session_id: null,
  summary: [],
  ...(enrolment === undefined ? {} : { enrolment }),
});

// The same key with non-zero unused trailing bits in its last character: it decodes to the
// same 32 bytes under a lax decoder, so only a strict (canonical) decoder rejects it.
const nonCanonical = (b64url) => {
  const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  const last = ALPHABET.indexOf(b64url.at(-1));
  if ((last & 3) !== 0) throw new Error("expected a canonical 43-character key");
  return b64url.slice(0, -1) + ALPHABET[last | 1];
};

const enrolmentCase = (name, welcome, expect, reason) => ({
  name,
  check: "enrolment_welcome",
  welcome,
  core_fingerprint: fingerprintOf(KEYS["core-1"].pubRaw),
  expect,
  reason,
});

// ---------------------------------------------------------------- seq cases

// A fresh checker per case; every value is fed in order. Accept: all pass.
// Reject: the last value fails with `bad_seq`, and every earlier one passes.
const seqCase = (name, seqs, expect) => ({
  name,
  check: "seq",
  seqs,
  expect,
  reason: expect === "accept" ? null : "bad_seq",
});

const authzCases = () => [
  authzCase("accept-valid", "2026-11-22T10:01:00Z", authzOf(actionObject()), "accept", null),
  authzCase(
    "reject-gateway-key-only",
    "2026-11-22T10:01:00Z",
    authzOf(actionObject(), { keyLabel: "gw-1", keyId: "gw-1" }),
    "reject",
    "untrusted_key_id",
  ),
  authzCase(
    "reject-gateway-key-under-core-key-id",
    "2026-11-22T10:01:00Z",
    authzOf(actionObject(), { keyLabel: "gw-1", keyId: "core-1" }),
    "reject",
    "bad_signature",
  ),
  authzCase(
    "reject-wrong-purpose",
    "2026-11-22T10:01:00Z",
    authzOf(actionObject(), { purpose: "gw-transport" }),
    "reject",
    "bad_signature",
  ),
  authzCase(
    "reject-other-target-node",
    "2026-11-22T10:01:00Z",
    authzOf(actionObject({ target_node_id: OTHER_NODE })),
    "reject",
    "wrong_target",
  ),
  authzCase("reject-expired", "2026-11-22T10:02:31Z", authzOf(actionObject()), "reject", "expired"),
  authzCase("accept-within-tolerance", "2026-11-22T10:02:29Z", authzOf(actionObject()), "accept", null),
  authzCase("accept-at-tolerance-limit", "2026-11-22T10:02:30Z", authzOf(actionObject()), "accept", null),
  authzCase(
    "reject-untrusted-key-id",
    "2026-11-22T10:01:00Z",
    authzOf(actionObject(), { keyLabel: "unknown-1", keyId: "core-9" }),
    "reject",
    "untrusted_key_id",
  ),
  authzCase("reject-tampered-args", "2026-11-22T10:01:00Z", tampered(), "reject", "bad_signature"),
  authzCase(
    "reject-naive-valid-until",
    "2026-11-22T10:01:00Z",
    authzOf(actionObject({ valid_until: "2026-11-22T10:02:00" })),
    "reject",
    "malformed",
  ),
  authzCase(
    "reject-card-as-action",
    "2026-11-22T10:01:00Z",
    authzOf(actionObject({ kind: "approval.card", reply_id: null, summary: "Delete a test file" })),
    "reject",
    "wrong_kind",
  ),
  // keyset (valid_until 2026-11-23T10:00:00Z)
  keysetCase("accept-keyset", "2026-11-22T10:01:00Z", NEW_CORE_KEY, "accept", null),
  keysetCase("reject-keyset-expired", "2026-11-23T10:00:31Z", NEW_CORE_KEY, "reject", "expired"),
  keysetCase("accept-keyset-within-tolerance", "2026-11-23T10:00:29Z", NEW_CORE_KEY, "accept", null),
  keysetCase(
    "reject-keyset-payload-mismatch",
    "2026-11-22T10:01:00Z",
    { ...NEW_CORE_KEY, public_key: b64("unknown-1") },
    "reject",
    "keyset_mismatch",
  ),
  keysetCase(
    "reject-keyset-naive-valid-until",
    "2026-11-22T10:01:00Z",
    NEW_CORE_KEY,
    "reject",
    "malformed",
    { valid_until: "2026-11-23T10:00:00" },
  ),
  // keyset target (R12-27) and role (R6-3): S-9
  keysetCase("reject-keyset-other-target", "2026-11-22T10:01:00Z", NEW_CORE_KEY, "reject", "wrong_target", {
    target_node_id: OTHER_NODE,
  }),
  keysetCase("accept-keyset-for-this-node", "2026-11-22T10:01:00Z", NEW_CORE_KEY, "accept", null, {
    target_node_id: ME,
  }),
  keysetCase(
    "accept-keyset-gateway-role",
    "2026-11-22T10:01:00Z",
    NEW_GATEWAY_KEY,
    "accept",
    null,
    {},
    NEW_GATEWAY_KEY,
  ),
  // challenge
  challengeCase("accept-challenge", challengeEnvelope(), PINNED_GW, null, "accept", null),
  challengeCase(
    "accept-challenge-enrol-by-fingerprint",
    challengeEnvelope(),
    null,
    fingerprintOf(KEYS["gw-1"].pubRaw),
    "accept",
    null,
  ),
  challengeCase(
    "reject-challenge-unpinned-key",
    challengeEnvelope({ keyLabel: "unknown-1", keyId: "gw-2" }),
    PINNED_GW,
    null,
    "reject",
    "unpinned_key",
  ),
  challengeCase(
    "reject-challenge-fingerprint-mismatch",
    challengeEnvelope(),
    null,
    fingerprintOf(KEYS["unknown-1"].pubRaw),
    "reject",
    "fingerprint_mismatch",
  ),
  challengeCase("reject-challenge-bad-signature", tamperedChallenge(), PINNED_GW, null, "reject", "bad_signature"),
  challengeCase(
    "reject-challenge-signed-by-core-key",
    challengeEnvelope({ keyLabel: "core-1", keyId: "gw-1", claimedLabel: "gw-1" }),
    PINNED_GW,
    null,
    "reject",
    "bad_signature",
  ),
  challengeCase(
    "reject-challenge-naive-time",
    challengeEnvelope({ issuedAt: "2026-11-22T10:00:00" }),
    PINNED_GW,
    null,
    "reject",
    "malformed",
  ),
  // enrolment welcome
  enrolmentCase(
    "accept-enrolment-welcome",
    welcomeWith({ node_id: ME, core_key_id: "core-1", core_public_key: b64("core-1") }),
    "accept",
    null,
  ),
  enrolmentCase(
    "reject-enrolment-core-fingerprint-mismatch",
    welcomeWith({ node_id: ME, core_key_id: "core-1", core_public_key: b64("gw-1") }),
    "reject",
    "core_fingerprint_mismatch",
  ),
  enrolmentCase("reject-enrolment-block-missing", welcomeWith(undefined), "reject", "enrolment_missing"),
  enrolmentCase(
    "reject-non-canonical-base64url-key",
    welcomeWith({ node_id: ME, core_key_id: "core-1", core_public_key: nonCanonical(b64("core-1")) }),
    "reject",
    "malformed_key",
  ),
  // seq
  seqCase("accept-consecutive", [1, 2, 3, 4], "accept"),
  seqCase("reject-gap", [1, 2, 4], "reject"),
  seqCase("reject-repeat", [1, 2, 2], "reject"),
  seqCase("reject-decrease", [1, 2, 3, 2], "reject"),
  seqCase("reject-first-not-1", [2], "reject"),
];

// ---------------------------------------------------------------- main

const which = process.argv[2];
const outputs = { vectors, "authz-cases": authzCases };
if (!(which in outputs)) {
  process.stderr.write("usage: node generate.mjs vectors|authz-cases\n");
  process.exit(2);
}
process.stdout.write(`${JSON.stringify(outputs[which](), null, 2)}\n`);
