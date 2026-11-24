//! The enrolment code Manj types into the desktop (§7.2 step 1): the one-time token plus the
//! core and gateway key fingerprints, as `token.corefp.gwfp`. It is never logged or stored.

/// A parsed enrolment code. `Debug` hides the token.
#[derive(Clone, PartialEq, Eq)]
pub struct EnrolmentCode {
    pub token: String,
    pub core_fp: String,
    pub gateway_fp: String,
}

impl std::fmt::Debug for EnrolmentCode {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("EnrolmentCode")
            .field("token", &"<hidden>")
            .field("core_fp", &self.core_fp)
            .field("gateway_fp", &self.gateway_fp)
            .finish()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("that is not a valid enrolment code")]
pub struct CodeError;

/// Lower-case base32 (`[a-z2-7]`) of exactly `len` characters.
fn base32_lower(text: &str, len: usize) -> bool {
    text.len() == len && text.bytes().all(|b| matches!(b, b'a'..=b'z' | b'2'..=b'7'))
}

impl EnrolmentCode {
    /// `token.corefp.gwfp`: a 26-character token and two 16-character fingerprints, as
    /// `jarvis_server.nodes.parse_enrolment_code` defines. Surrounding whitespace is ignored.
    pub fn parse(code: &str) -> Result<Self, CodeError> {
        let parts: Vec<&str> = code.trim().split('.').collect();
        match parts.as_slice() {
            [token, core, gw]
                if base32_lower(token, 26) && base32_lower(core, 16) && base32_lower(gw, 16) =>
            {
                Ok(Self {
                    token: (*token).to_owned(),
                    core_fp: (*core).to_owned(),
                    gateway_fp: (*gw).to_owned(),
                })
            }
            _ => Err(CodeError),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn code_parse_accepts_and_rejects() {
        let token = "a".repeat(26);
        let (core, gw) = ("b".repeat(16), "c2".repeat(8));
        let ok = EnrolmentCode::parse(&format!("  {token}.{core}.{gw}\n")).unwrap();
        assert_eq!(
            (ok.token.as_str(), ok.core_fp.as_str()),
            (token.as_str(), core.as_str())
        );
        assert!(
            !format!("{ok:?}").contains(&token),
            "Debug never shows the token"
        );
        for bad in [
            String::new(),
            format!("{token}.{core}"),
            format!("{token}.{core}.{gw}.x"),
            format!("{}.{core}.{gw}", "a".repeat(25)),
            format!("{token}.{}.{gw}", "b".repeat(15)),
            format!("{}.{core}.{gw}", "A".repeat(26)),
            format!("{token}.{core}.{}", "1".repeat(16)),
            format!("{token} .{core}.{gw}"),
        ] {
            assert_eq!(EnrolmentCode::parse(&bad), Err(CodeError), "{bad:?}");
        }
    }
}
