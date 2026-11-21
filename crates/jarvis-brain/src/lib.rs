//! Brain implementations (ADR-002).

pub mod assemble;
pub mod claude;
pub mod eval;
pub mod sse;

pub use claude::ClaudeBrain;
