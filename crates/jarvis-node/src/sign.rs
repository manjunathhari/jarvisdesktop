//! Canonical signing (spec §7.1).
//!
//! Signed bytes are `"jarvis-sig-v1\n" + purpose + "\n" + key_id + "\n" + JCS(object)`, with JCS
//! from RFC 8785 (`serde_json_canonicalizer`, R12-35) and Ed25519 signatures in base64url
//! without padding. Signatures
//! are always checked over the JSON object exactly as received, never over a re-serialised
//! struct.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use ed25519_dalek::{Signature, Signer as _, SigningKey, VerifyingKey};
use serde_json::Value;
use sha2::{Digest as _, Sha256};

const PREFIX: &str = "jarvis-sig-v1\n";
const SIG_FIELD: &str = "sig";

/// The three signing purposes. The purpose is inside the signed bytes, so a signature made for
/// one never verifies as another.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Purpose {
    /// Every envelope a node sends, under its own key.
    NodeMsg,
    /// Every envelope the gateway sends. Transport only: never authorises an action.
    GwTransport,
    /// The `authz` object inside actions, approval cards and keysets, under a core key.
    CoreAuthz,
}

impl Purpose {
    pub fn as_str(self) -> &'static str {
        match self {
            Purpose::NodeMsg => "node-msg",
            Purpose::GwTransport => "gw-transport",
            Purpose::CoreAuthz => "core-authz",
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum SignError {
    #[error("key_id must be non-empty and contain no newline")]
    BadKeyId,
    #[error("cannot canonicalise: {0}")]
    Canonical(String),
    #[error("an envelope must be a JSON object")]
    NotAnObject,
    #[error("an integer is outside the safe integer range ±(2^53 - 1)")]
    UnsafeInteger,
}

/// The largest integer JCS (RFC 8785) can represent exactly: 2^53 - 1.
pub const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;

/// Refuse any integer outside ±(2^53 - 1), as Python's `rfc8785` does (`IntegerDomainError`).
///
/// `serde_json_canonicalizer` writes every integer through `f64`, so without this check two
/// different large integers could canonicalise to the same bytes and share one signature.
/// Integers beyond the 64-bit range already arrive from `serde_json` as floats and can't be
/// told apart from real floats (such as the RFC's own `1E30`); no JARVIS signer can produce
/// them, since every signed JARVIS number is a safe integer (§7.1). This relies on serde_json's
/// `arbitrary_precision` feature staying off: with it on, every large integer would look like
/// a float here and pass.
fn check_integers(v: &Value) -> Result<(), SignError> {
    match v {
        Value::Number(n) => {
            let safe = match (n.as_i64(), n.as_u64()) {
                (Some(i), _) => i.unsigned_abs() <= MAX_SAFE_INTEGER,
                (None, Some(u)) => u <= MAX_SAFE_INTEGER,
                (None, None) => true, // a float
            };
            if safe {
                Ok(())
            } else {
                Err(SignError::UnsafeInteger)
            }
        }
        Value::Array(items) => items.iter().try_for_each(check_integers),
        Value::Object(map) => map.values().try_for_each(check_integers),
        _ => Ok(()),
    }
}

/// RFC 8785 (JCS) serialisation: object keys sorted by their UTF-16 code units, as Python's
/// `rfc8785` does (vectors `rfc8785-sorting` and `key-order-utf16`). An integer outside
/// ±(2^53 - 1) is refused, so it can never be signed or verify.
pub fn canonical(obj: &Value) -> Result<Vec<u8>, SignError> {
    check_integers(obj)?;
    serde_json_canonicalizer::to_vec(obj).map_err(|e| SignError::Canonical(e.to_string()))
}

/// The exact bytes that are signed.
pub fn signed_bytes(purpose: Purpose, key_id: &str, obj: &Value) -> Result<Vec<u8>, SignError> {
    if key_id.is_empty() || key_id.contains('\n') {
        return Err(SignError::BadKeyId);
    }
    let mut out = format!("{PREFIX}{}\n{key_id}\n", purpose.as_str()).into_bytes();
    out.extend(canonical(obj)?);
    Ok(out)
}

/// An Ed25519 signature over [`signed_bytes`], base64url without padding.
pub fn sign(
    key: &SigningKey,
    purpose: Purpose,
    key_id: &str,
    obj: &Value,
) -> Result<String, SignError> {
    let bytes = signed_bytes(purpose, key_id, obj)?;
    Ok(b64url(&key.sign(&bytes).to_bytes()))
}

/// True only for a valid signature. Never panics on a bad signature, key or key_id.
///
/// Uses Ed25519 strict verification (canonical encodings, no small-order keys), so it can only
/// refuse more than a lax verifier, never accept more.
pub fn verify(
    public_key: &[u8; 32],
    purpose: Purpose,
    key_id: &str,
    obj: &Value,
    sig: &str,
) -> bool {
    let Some(raw) = b64url_decode(sig, 64) else {
        return false;
    };
    let Ok(raw): Result<[u8; 64], _> = raw.try_into() else {
        return false;
    };
    let Ok(public) = VerifyingKey::from_bytes(public_key) else {
        return false;
    };
    let Ok(bytes) = signed_bytes(purpose, key_id, obj) else {
        return false;
    };
    public
        .verify_strict(&bytes, &Signature::from_bytes(&raw))
        .is_ok()
}

/// Sign an envelope in place: `sig` covers every other field.
pub fn sign_envelope(
    key: &SigningKey,
    purpose: Purpose,
    key_id: &str,
    env: &mut Value,
) -> Result<(), SignError> {
    let map = env.as_object_mut().ok_or(SignError::NotAnObject)?;
    map.remove(SIG_FIELD);
    let sig = sign(key, purpose, key_id, &Value::Object(map.clone()))?;
    map.insert(SIG_FIELD.to_owned(), Value::String(sig));
    Ok(())
}

/// True only if the envelope's `sig` is valid over every other field.
pub fn verify_envelope(public_key: &[u8; 32], purpose: Purpose, key_id: &str, env: &Value) -> bool {
    let Some(map) = env.as_object() else {
        return false;
    };
    let Some(Value::String(sig)) = map.get(SIG_FIELD) else {
        return false;
    };
    let mut unsigned = map.clone();
    unsigned.remove(SIG_FIELD);
    verify(public_key, purpose, key_id, &Value::Object(unsigned), sig)
}

/// A public key's fingerprint: the first 10 bytes of SHA-256, lower-case base32 without
/// padding (16 characters). A node's envelope `key_id` is its own fingerprint (R12-6).
pub fn fingerprint(public_key: &[u8; 32]) -> String {
    let digest = Sha256::digest(public_key);
    data_encoding::BASE32_NOPAD
        .encode(&digest[..10])
        .to_ascii_lowercase()
}

/// Base64url without padding.
pub fn b64url(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}

/// Exactly `len` bytes, or `None` unless `text` is their canonical unpadded base64url.
///
/// Strict, as the Python reference: no padding, only the URL-safe alphabet, and no non-zero
/// unused trailing bits (checked by re-encoding), so every value has exactly one spelling.
pub fn b64url_decode(text: &str, len: usize) -> Option<Vec<u8>> {
    let raw = URL_SAFE_NO_PAD.decode(text).ok()?;
    (raw.len() == len && b64url(&raw) == text).then_some(raw)
}
