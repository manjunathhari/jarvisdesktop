//! Slice-1 modules: news, live TV and built-in screen actions (ADR-003).

pub mod config;
pub mod fetch;
pub mod live_tv;
pub mod news;
pub mod screen_actions;
pub mod text;

pub use config::{Channel, FeedSource, Region, SourcesConfig};
pub use fetch::{Fetch, HttpFetch};
pub use live_tv::LiveTvModule;
pub use news::NewsModule;
pub use screen_actions::ScreenModule;

#[cfg(test)]
pub(crate) mod testutil {
    use crate::fetch::Fetch;
    use async_trait::async_trait;
    use std::collections::HashMap;
    use std::sync::Mutex;

    /// Serves fixture files by URL; unknown URLs fail like a dead site.
    #[derive(Default)]
    pub struct FixtureFetch {
        pub pages: Mutex<HashMap<String, Vec<u8>>>,
        pub calls: Mutex<Vec<String>>,
    }

    impl FixtureFetch {
        pub fn with(pages: &[(&str, &str)]) -> Self {
            let f = Self::default();
            for (url, file) in pages {
                let path = format!("{}/tests/fixtures/{file}", env!("CARGO_MANIFEST_DIR"));
                f.pages
                    .lock()
                    .unwrap()
                    .insert(url.to_string(), std::fs::read(path).unwrap());
            }
            f
        }
        pub fn remove(&self, url: &str) {
            self.pages.lock().unwrap().remove(url);
        }
    }

    #[async_trait]
    impl Fetch for FixtureFetch {
        async fn get(&self, url: &str) -> Result<Vec<u8>, String> {
            self.calls.lock().unwrap().push(url.to_string());
            self.pages
                .lock()
                .unwrap()
                .get(url)
                .cloned()
                .ok_or_else(|| format!("404 {url}"))
        }
    }
}
