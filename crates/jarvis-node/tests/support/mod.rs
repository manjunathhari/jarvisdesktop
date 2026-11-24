//! Shared test helpers: the committed contracts and signing vectors, read-only.
#![allow(dead_code)] // each test crate uses a different subset

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

/// A test key from the vectors' fixed seeds, by its vector `key_id` (e.g. `core-1`).
pub fn test_key(label: &str) -> SigningKey {
    let seed = vectors()
        .into_iter()
        .find(|v| v["key_id"] == label)
        .and_then(|v| v["seed_hex"].as_str().map(str::to_owned))
        .unwrap_or_else(|| panic!("no vector with key_id {label}"));
    let raw: [u8; 32] = hex::decode(seed).unwrap().try_into().unwrap();
    SigningKey::from_bytes(&raw)
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
