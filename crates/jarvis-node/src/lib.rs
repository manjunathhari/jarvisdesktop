//! The JARVIS desktop node (spec §13, Plan B).
//!
//! Platform-neutral protocol code: canonical signing (`sign`), the wire types the signed
//! authorisation objects use (`wire`), and the node-side rules for them (`rules`). The rules
//! match the Python reference in `server/tests/fakes/node_rules.py` and are checked against the
//! committed shared cases in `contracts/signing-vectors/`, which Rust never writes.
//!
//! Local durability: the append-only `journal`, the node's `state` (node ID and pinned keys,
//! in `trust`), and the `keystore` for its private key (DPAPI on Windows).

pub mod approval;
pub mod client;
pub mod code;
pub mod delivery;
pub mod journal;
pub mod keystore;
pub mod node;
pub mod rules;
pub mod server_brain;
pub mod session;
pub mod sign;
pub mod state;
pub mod trust;
pub mod wire;
