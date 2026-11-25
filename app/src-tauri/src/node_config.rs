//! `node.toml` in the app's local data folder: which brain answers, and the server node's
//! settings (plan B Task 9). Nothing here is a secret, and none of it is in the repo.
//!
//! ```toml
//! brain = "server"            # or "claude" (today's behaviour, and the default)
//! gateway = "host:port"       # required for "server"
//! test_folder = 'C:\Users\you\JarvisTest'   # optional; absolute
//! ```

use std::path::{Path, PathBuf};

use serde::Deserialize;

pub const FILE: &str = "node.toml";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BrainChoice {
    /// The desktop's own Claude brain, with the key in Credential Manager.
    Claude,
    /// The Jarvis server, through the node.
    Server,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeConfig {
    pub brain: BrainChoice,
    pub gateway: Option<String>,
    pub test_folder: Option<PathBuf>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Raw {
    brain: Option<BrainChoice>,
    gateway: Option<String>,
    test_folder: Option<PathBuf>,
}

impl Default for NodeConfig {
    fn default() -> Self {
        Self {
            brain: BrainChoice::Claude,
            gateway: None,
            test_folder: None,
        }
    }
}

/// `dir/node.toml`. A missing file means today's behaviour (the Claude brain); a file that
/// exists but is wrong is an error, never a silent fallback.
pub fn load(dir: &Path) -> Result<NodeConfig, String> {
    let text = match std::fs::read_to_string(dir.join(FILE)) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(NodeConfig::default()),
        Err(e) => return Err(format!("couldn't read {FILE}: {e}")),
    };
    parse(&text)
}

pub fn parse(text: &str) -> Result<NodeConfig, String> {
    let raw: Raw = toml::from_str(text).map_err(|e| format!("{FILE}: {e}"))?;
    let brain = raw.brain.unwrap_or(BrainChoice::Claude);
    let gateway = raw.gateway.map(|g| g.trim().to_owned());
    if let Some(g) = &gateway {
        if !valid_host_port(g) {
            return Err(format!("{FILE}: gateway must be host:port"));
        }
    }
    if brain == BrainChoice::Server && gateway.is_none() {
        return Err(format!(
            "{FILE}: brain = \"server\" needs a gateway (host:port)"
        ));
    }
    if let Some(folder) = &raw.test_folder {
        if !is_absolute(folder) {
            return Err(format!("{FILE}: test_folder must be an absolute path"));
        }
    }
    Ok(NodeConfig {
        brain,
        gateway,
        test_folder: raw.test_folder,
    })
}

/// `host:port`: a non-empty host without a scheme, path or spaces, and a port 1–65535.
fn valid_host_port(s: &str) -> bool {
    let Some((host, port)) = s.rsplit_once(':') else {
        return false;
    };
    let host_ok = !host.is_empty()
        && !host.contains("://")
        && !host.contains(['/', ' ', '@'])
        && (host.starts_with('[') == host.ends_with(']'));
    host_ok && port.parse::<u16>().is_ok_and(|p| p > 0)
}

/// Absolute on Windows (`C:\…`) as well as wherever the tests run.
fn is_absolute(p: &Path) -> bool {
    if p.is_absolute() {
        return true;
    }
    let s = p.to_string_lossy();
    let b = s.as_bytes();
    b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && (b[2] == b'\\' || b[2] == b'/')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_missing_file_keeps_the_claude_brain() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(load(dir.path()).unwrap(), NodeConfig::default());
        assert_eq!(load(dir.path()).unwrap().brain, BrainChoice::Claude);
    }

    #[test]
    fn server_mode_needs_a_gateway() {
        assert!(parse("brain = \"server\"")
            .unwrap_err()
            .contains("needs a gateway"));
        let c = parse("brain = \"server\"\ngateway = \"jarvis-gw.example:8443\"").unwrap();
        assert_eq!(
            (c.brain, c.gateway.as_deref()),
            (BrainChoice::Server, Some("jarvis-gw.example:8443"))
        );
    }

    #[test]
    fn a_relative_test_folder_is_an_error() {
        let base = "brain = \"server\"\ngateway = \"gw.example:1\"\n";
        assert!(parse(&format!("{base}test_folder = 'JarvisTest'")).is_err());
        let ok = parse(&format!("{base}test_folder = 'C:\\Users\\m\\JarvisTest'")).unwrap();
        assert_eq!(
            ok.test_folder,
            Some(PathBuf::from(r"C:\Users\m\JarvisTest"))
        );
    }

    #[test]
    fn bad_values_are_errors_not_fallbacks() {
        for bad in [
            "brain = \"gpt\"",
            "brain = \"server\"\ngateway = \"ws://gw.example:1\"",
            "brain = \"server\"\ngateway = \"gw.example\"",
            "brain = \"server\"\ngateway = \"gw.example:0\"",
            "brain = \"server\"\ngateway = \"gw.example:99999\"",
            "brain = \"server\"\ngateway = \"[fd00::1:443\"",
            "brian = \"server\"",
            "not toml",
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
        assert_eq!(parse("").unwrap(), NodeConfig::default());
        assert!(parse("brain = \"server\"\ngateway = \"[fd00::1]:443\"").is_ok());
    }
}
