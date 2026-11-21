//! The Anthropic key: asked for once in the app, kept in Windows Credential
//! Manager. Never written to a file.

#[cfg_attr(not(windows), allow(dead_code))]
const SERVICE: &str = "JARVIS Desktop";
#[cfg_attr(not(windows), allow(dead_code))]
const ACCOUNT: &str = "anthropic-api-key";

/// Tidy and sanity-check a pasted key.
pub fn clean_key(input: &str) -> Result<String, String> {
    let k = input.trim().trim_matches(|c| c == '"' || c == '\'').trim();
    if !k.starts_with("sk-ant-") {
        return Err(
            "That doesn't look like an Anthropic key. It should start with sk-ant-.".into(),
        );
    }
    if k.len() < 30 || k.chars().any(char::is_whitespace) {
        return Err("That key looks incomplete. Copy it again from the Anthropic console.".into());
    }
    Ok(k.to_string())
}

/// The key, from Credential Manager, or the environment (for development).
pub fn load() -> Option<String> {
    if let Some(k) = std::env::var("ANTHROPIC_API_KEY")
        .ok()
        .filter(|k| !k.trim().is_empty())
    {
        return Some(k);
    }
    stored()
}

#[cfg(windows)]
fn stored() -> Option<String> {
    keyring::Entry::new(SERVICE, ACCOUNT)
        .ok()?
        .get_password()
        .ok()
}

#[cfg(not(windows))]
fn stored() -> Option<String> {
    let _ = (SERVICE, ACCOUNT);
    None
}

#[cfg(windows)]
pub fn save(key: &str) -> Result<(), String> {
    keyring::Entry::new(SERVICE, ACCOUNT)
        .and_then(|e| e.set_password(key))
        .map_err(|e| format!("Couldn't save the key: {e}"))
}

#[cfg(not(windows))]
pub fn save(_key: &str) -> Result<(), String> {
    Err("Saving the key is only supported on Windows; set ANTHROPIC_API_KEY instead.".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_a_pasted_key_and_trims_it() {
        let k = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789";
        assert_eq!(clean_key(&format!("  {k}\n")), Ok(k.to_string()));
    }

    #[test]
    fn strips_quotes_people_copy_along_with_it() {
        let k = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789";
        assert_eq!(clean_key(&format!("\"{k}\"")), Ok(k.to_string()));
    }

    #[test]
    fn rejects_things_that_are_not_an_anthropic_key() {
        assert!(clean_key("").is_err());
        assert!(clean_key("paste-your-key-here").is_err());
        assert!(clean_key("sk-ant-short").is_err());
        assert!(
            clean_key("sk-ant-api03-abc def ghijklmnopqrstuvwxyz0123").is_err(),
            "inner space"
        );
    }
}
