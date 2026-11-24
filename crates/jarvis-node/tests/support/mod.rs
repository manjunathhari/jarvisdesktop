//! Shared test helpers: the committed contracts and signing vectors, read-only.
#![allow(dead_code)] // each test crate uses a different subset

pub mod authz;
pub mod gateway;
pub mod net;

use std::path::PathBuf;

use ed25519_dalek::SigningKey;
use serde_json::Value;

/// The repo's `contracts/` folder.
pub fn contracts() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../contracts")
}

fn read_json(path: PathBuf) -> Value {
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn list(path: PathBuf) -> Vec<Value> {
    match read_json(path) {
        Value::Array(items) => items,
        other => panic!("expected a list, got {other}"),
    }
}

/// `contracts/signing-vectors/authz-cases.json`.
pub fn cases() -> Vec<Value> {
    list(contracts().join("signing-vectors/authz-cases.json"))
}

/// `contracts/signing-vectors/vectors.json`.
pub fn vectors() -> Vec<Value> {
    list(contracts().join("signing-vectors/vectors.json"))
}

pub fn case(name: &str) -> Value {
    cases()
        .into_iter()
        .find(|c| c["name"] == name)
        .unwrap_or_else(|| panic!("no shared case {name}"))
}

/// A test key by its generator label (`core-1`, `core-2`, `gw-1`, `gw-2`, `node-1`,
/// `unknown-1`): the seed is SHA-256 of `jarvis-signing-vectors test seed: <label>`, the rule
/// `contracts/signing-vectors/generate.mjs` publishes. `test_keys_match_the_vectors` checks it.
pub fn test_key(label: &str) -> SigningKey {
    use sha2::{Digest, Sha256};
    let seed: [u8; 32] =
        Sha256::digest(format!("jarvis-signing-vectors test seed: {label}")).into();
    SigningKey::from_bytes(&seed)
}

/// A JSON message example from `contracts/examples/messages/`.
pub fn example(name: &str) -> Value {
    read_json(contracts().join("examples/messages").join(name))
}

/// Panics unless `payload` matches `contracts/messages/<kind>.schema.json`, with sibling
/// schemas resolved by their `$id` and formats (`uuid`, `date-time`) checked.
pub fn assert_conforms(kind: &str, payload: &Value) {
    let dir = contracts().join("messages");
    let mut resources = Vec::new();
    for entry in std::fs::read_dir(&dir).unwrap() {
        let schema = read_json(entry.unwrap().path());
        let id = schema["$id"]
            .as_str()
            .expect("every schema has an $id")
            .to_owned();
        resources.push((id, jsonschema::Resource::from_contents(schema).unwrap()));
    }
    let schema = read_json(dir.join(format!("{kind}.schema.json")));
    let validator = jsonschema::options()
        .with_draft(jsonschema::Draft::Draft202012)
        .should_validate_formats(true)
        .with_resources(resources.into_iter())
        .build(&schema)
        .unwrap_or_else(|e| panic!("{kind} schema: {e}"));
    let errors: Vec<String> = validator
        .iter_errors(payload)
        .map(|e| e.to_string())
        .collect();
    assert!(
        errors.is_empty(),
        "{kind} payload does not conform: {errors:?}\n{payload}"
    );
}

/// Whether `payload` matches the schema, for negative checks.
pub fn conforms(kind: &str, payload: &Value) -> bool {
    std::panic::catch_unwind(|| assert_conforms(kind, payload)).is_ok()
}

/// Panics unless `env` matches `contracts/node-envelope.schema.json`.
pub fn assert_envelope_conforms(env: &Value) {
    let schema = read_json(contracts().join("node-envelope.schema.json"));
    let validator = jsonschema::options()
        .with_draft(jsonschema::Draft::Draft202012)
        .should_validate_formats(true)
        .build(&schema)
        .unwrap();
    let errors: Vec<String> = validator.iter_errors(env).map(|e| e.to_string()).collect();
    assert!(
        errors.is_empty(),
        "envelope does not conform: {errors:?}\n{env}"
    );
}
