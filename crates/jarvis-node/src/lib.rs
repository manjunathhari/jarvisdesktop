//! The JARVIS desktop node (spec §13, Plan B).
//!
//! Platform-neutral protocol code: canonical signing (`sign`), the wire types the signed
//! authorisation objects use (`wire`), and the node-side rules for them (`rules`). The rules
//! match the Python reference in `server/tests/fakes/node_rules.py` and are checked against the
//! committed shared cases in `contracts/signing-vectors/`, which Rust never writes.

pub mod rules;
pub mod sign;
pub mod wire;
