//! The node's pinned keys (spec §7.1): CORE keys, which authorise, and GATEWAY keys, which
//! only carry transport. They are two separate maps, so a key pinned for one role is never
//! found when the other is asked for (R6-3).
//!
//! This module only stores, prunes and looks up. Pinning at enrolment (Task 4) and keyset
//! rotation (Task 5) build on it.

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset};
use serde::{Deserialize, Serialize};

use crate::rules::CoreKeys;

/// A pinned public key. `expires_at` is set while the key is in a rotation overlap: it is
/// trusted until then, and dropped after.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PinnedKey {
    #[serde(with = "key_b64url")]
    pub public_key: [u8; 32],
    pub expires_at: Option<DateTime<FixedOffset>>,
}

impl PinnedKey {
    /// A key with no expiry.
    pub fn new(public_key: [u8; 32]) -> Self {
        Self {
            public_key,
            expires_at: None,
        }
    }

    /// Trusted at `now`: no expiry, or `now` not past it (as FakeNode's `_prune`).
    fn live(&self, now: DateTime<FixedOffset>) -> bool {
        self.expires_at.is_none_or(|until| now <= until)
    }
}

/// CORE and GATEWAY pins, `key_id` → key.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Trust {
    pub core: BTreeMap<String, PinnedKey>,
    pub gateway: BTreeMap<String, PinnedKey>,
}

impl Trust {
    /// Drop every key whose overlap has ended (`now` > `expires_at`), from both maps.
    pub fn prune(&mut self, now: DateTime<FixedOffset>) {
        self.core.retain(|_, k| k.live(now));
        self.gateway.retain(|_, k| k.live(now));
    }

    /// The CORE keys trusted at `now`, for the authorisation rules. Never a gateway key.
    pub fn core_keys(&self, now: DateTime<FixedOffset>) -> CoreKeys {
        self.core
            .iter()
            .filter(|(_, k)| k.live(now))
            .map(|(id, k)| (id.clone(), k.public_key))
            .collect()
    }

    /// The GATEWAY key pinned under `key_id` and trusted at `now`. Never a core key.
    pub fn gateway_key(&self, key_id: &str, now: DateTime<FixedOffset>) -> Option<[u8; 32]> {
        self.gateway
            .get(key_id)
            .filter(|k| k.live(now))
            .map(|k| k.public_key)
    }
}

/// A raw 32-byte key as strict base64url text, as on the wire.
mod key_b64url {
    use serde::{de::Error as _, Deserialize, Deserializer, Serializer};

    use crate::sign::{b64url, b64url_decode};

    pub fn serialize<S: Serializer>(key: &[u8; 32], s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&b64url(key))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<[u8; 32], D::Error> {
        let text = String::deserialize(d)?;
        b64url_decode(&text, 32)
            .and_then(|raw| raw.try_into().ok())
            .ok_or_else(|| D::Error::custom("not a canonical base64url 32-byte key"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(text: &str) -> DateTime<FixedOffset> {
        DateTime::parse_from_rfc3339(text).unwrap()
    }

    fn trust() -> Trust {
        let mut trust = Trust::default();
        trust.core.insert("core-1".into(), PinnedKey::new([1; 32]));
        trust.core.insert(
            "core-0".into(),
            PinnedKey {
                public_key: [2; 32],
                expires_at: Some(t("2026-11-25T10:00:00Z")),
            },
        );
        trust.gateway.insert("gw-1".into(), PinnedKey::new([3; 32]));
        trust
    }

    #[test]
    fn pruned_keys_are_not_trusted() {
        let mut trust = trust();
        let before = t("2026-11-25T10:00:00Z"); // at the expiry: still trusted
        let after = t("2026-11-25T10:00:00.001Z");
        assert_eq!(trust.core_keys(before).len(), 2);
        assert_eq!(
            trust.core_keys(after).len(),
            1,
            "expired keys are never returned"
        );
        trust.prune(before);
        assert!(trust.core.contains_key("core-0"));
        trust.prune(after);
        assert!(!trust.core.contains_key("core-0"));
        assert!(
            trust.core.contains_key("core-1"),
            "keys without expiry stay"
        );
        assert!(trust.gateway.contains_key("gw-1"));
    }

    #[test]
    fn roles_never_cross() {
        let trust = trust();
        let now = t("2026-11-24T10:00:00Z");
        assert_eq!(trust.gateway_key("gw-1", now), Some([3; 32]));
        assert_eq!(
            trust.gateway_key("core-1", now),
            None,
            "a core key is not a gateway key"
        );
        assert!(
            !trust.core_keys(now).contains_key("gw-1"),
            "a gateway key is not a core key"
        );
    }

    #[test]
    fn gateway_keys_expire_too() {
        let mut trust = Trust::default();
        trust.gateway.insert(
            "gw-0".into(),
            PinnedKey {
                public_key: [4; 32],
                expires_at: Some(t("2026-11-25T10:00:00Z")),
            },
        );
        assert!(trust
            .gateway_key("gw-0", t("2026-11-25T09:59:59Z"))
            .is_some());
        assert!(trust
            .gateway_key("gw-0", t("2026-11-25T10:00:01Z"))
            .is_none());
        trust.prune(t("2026-11-25T10:00:01Z"));
        assert!(trust.gateway.is_empty());
    }

    #[test]
    fn serialises_keys_as_base64url_and_refuses_bad_ones() {
        let json = serde_json::to_value(trust()).unwrap();
        assert_eq!(
            json["core"]["core-1"]["public_key"],
            crate::sign::b64url(&[1; 32])
        );
        assert_eq!(
            serde_json::from_value::<Trust>(json.clone()).unwrap(),
            trust()
        );
        let mut bad = json.clone();
        bad["core"]["core-1"]["public_key"] = "AAAA".into();
        assert!(serde_json::from_value::<Trust>(bad).is_err());
        let mut extra = json;
        extra["core"]["core-1"]["role"] = "core".into();
        assert!(serde_json::from_value::<Trust>(extra).is_err());
    }
}
