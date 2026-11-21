//! Network access behind a trait, so tests never touch the internet.

use async_trait::async_trait;
use std::time::Duration;

#[async_trait]
pub trait Fetch: Send + Sync {
    async fn get(&self, url: &str) -> Result<Vec<u8>, String>;
}

pub struct HttpFetch {
    client: reqwest::Client,
}

impl HttpFetch {
    pub fn new() -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .user_agent("JARVIS-Desktop/0.1 (+https://github.com/manjunathhari/jarvisdesktop)")
            .build()
            .expect("http client");
        Self { client }
    }
}

impl Default for HttpFetch {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Fetch for HttpFetch {
    async fn get(&self, url: &str) -> Result<Vec<u8>, String> {
        let resp = self
            .client
            .get(url)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if !resp.status().is_success() {
            return Err(format!("HTTP {}", resp.status()));
        }
        resp.bytes()
            .await
            .map(|b| b.to_vec())
            .map_err(|e| e.to_string())
    }
}
