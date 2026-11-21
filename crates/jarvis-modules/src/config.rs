//! `config/sources.toml`: news feeds and live channels.

use serde::Deserialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Region {
    India,
    World,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct FeedSource {
    pub name: String,
    pub url: String,
    pub region: Region,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Channel {
    pub name: String,
    pub youtube_channel_id: String,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct SourcesConfig {
    pub feeds: Vec<FeedSource>,
    pub channels: Vec<Channel>,
}

/// The copy shipped with the app.
pub const DEFAULT_SOURCES: &str = include_str!("../../../config/sources.toml");

impl SourcesConfig {
    pub fn parse(text: &str) -> Result<Self, String> {
        let cfg: Self = toml::from_str(text).map_err(|e| e.to_string())?;
        if cfg.channels.is_empty() {
            return Err("at least one live channel is required".into());
        }
        Ok(cfg)
    }

    pub fn default_config() -> Self {
        Self::parse(DEFAULT_SOURCES).expect("shipped sources.toml is valid")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shipped_config_has_indian_and_world_feeds_and_channels() {
        let cfg = SourcesConfig::default_config();
        assert!(cfg.feeds.iter().any(|f| f.region == Region::India));
        assert!(cfg.feeds.iter().any(|f| f.region == Region::World));
        assert!(!cfg.channels.is_empty());
        assert!(cfg.feeds.iter().all(|f| f.url.starts_with("https://")));
    }

    #[test]
    fn config_without_channels_is_rejected() {
        assert!(SourcesConfig::parse("feeds = []\nchannels = []").is_err());
    }
}
