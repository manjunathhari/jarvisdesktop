//! News module: background-refreshed headline cache → India / International panels.

use crate::config::{FeedSource, Region};
use crate::fetch::Fetch;
use crate::text::{plain_text, truncate_words};
use async_trait::async_trait;
use jarvis_core::action::{Action, ActionError, ActionSpec, Module, ModuleOutput};
use jarvis_core::screen::{Card, Panel, PanelContent, PanelUpdate, ScreenState, Slot};
use serde_json::json;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

pub const CARDS_PER_PANEL: usize = 6;
const SUMMARY_CHARS: usize = 280;

/// What JARVIS says when the wall appears: two Indian headlines and one
/// international (or two international when only world news was asked for).
fn briefing(india: &[Card], world: &[Card]) -> String {
    let line = |c: &Card| format!("From {}: {}.", c.source, c.title.trim_end_matches('.'));
    let mut parts = vec!["Here's the latest.".to_string()];
    parts.extend(india.iter().take(2).map(line));
    if india.is_empty() {
        parts.extend(world.iter().take(2).map(line));
    } else if let Some(w) = world.first() {
        parts.push(format!(
            "And internationally, {}",
            line(w).replacen("From", "from", 1)
        ));
    }
    parts.push("Say a number to hear more.".into());
    parts.join(" ")
}

/// Parse one feed into cards.
pub fn parse_feed(source: &FeedSource, bytes: &[u8]) -> Result<Vec<Card>, String> {
    let feed = feed_rs::parser::parse(bytes).map_err(|e| e.to_string())?;
    let cards = feed
        .entries
        .into_iter()
        .filter_map(|e| {
            let title = plain_text(&e.title.map(|t| t.content).unwrap_or_default());
            let link = e.links.first().map(|l| l.href.clone())?;
            if title.is_empty() {
                return None;
            }
            let raw = e
                .summary
                .map(|t| t.content)
                .or_else(|| e.content.and_then(|c| c.body))
                .unwrap_or_default();
            Some(Card {
                title,
                source: source.name.clone(),
                summary: truncate_words(&plain_text(&raw), SUMMARY_CHARS),
                link,
                published: e.published.or(e.updated).map(|d| d.to_rfc3339()),
            })
        })
        .collect();
    Ok(cards)
}

pub struct NewsModule {
    sources: Vec<FeedSource>,
    fetch: Arc<dyn Fetch>,
    cache: RwLock<HashMap<String, Vec<Card>>>,
}

impl NewsModule {
    pub fn new(sources: Vec<FeedSource>, fetch: Arc<dyn Fetch>) -> Self {
        Self {
            sources,
            fetch,
            cache: RwLock::new(HashMap::new()),
        }
    }

    /// Fetch every feed concurrently. A failing feed keeps its last good
    /// headlines. Returns the names of sources that failed this time.
    pub async fn refresh(&self) -> Vec<String> {
        let results = futures::future::join_all(self.sources.iter().map(|src| async move {
            let parsed = match self.fetch.get(&src.url).await {
                Ok(bytes) => parse_feed(src, &bytes),
                Err(e) => Err(e),
            };
            (src, parsed)
        }))
        .await;
        let mut failed = Vec::new();
        let mut cache = self.cache.write().await;
        for (src, parsed) in results {
            match parsed {
                Ok(cards) if !cards.is_empty() => {
                    cache.insert(src.name.clone(), cards);
                }
                Ok(_) => {
                    tracing::warn!(source = %src.name, "feed had no stories");
                    failed.push(src.name.clone());
                }
                Err(e) => {
                    tracing::warn!(source = %src.name, error = %e, "feed failed");
                    failed.push(src.name.clone());
                }
            }
        }
        failed
    }

    async fn cache_is_empty(&self) -> bool {
        self.cache.read().await.is_empty()
    }

    /// Up to `n` cards for a region, alternating between sources, no duplicate links.
    pub async fn headlines(&self, region: Region, topic: Option<&str>, n: usize) -> Vec<Card> {
        let cache = self.cache.read().await;
        let topic = topic.map(str::to_lowercase);
        let per_source: Vec<Vec<&Card>> = self
            .sources
            .iter()
            .filter(|s| s.region == region)
            .filter_map(|s| cache.get(&s.name))
            .map(|cards| {
                cards
                    .iter()
                    .filter(|c| match &topic {
                        Some(t) => {
                            c.title.to_lowercase().contains(t)
                                || c.summary.to_lowercase().contains(t)
                        }
                        None => true,
                    })
                    .collect()
            })
            .collect();
        let mut out: Vec<Card> = Vec::new();
        let longest = per_source.iter().map(Vec::len).max().unwrap_or(0);
        'outer: for i in 0..longest {
            for cards in &per_source {
                if let Some(c) = cards.get(i) {
                    if out.iter().all(|o| o.link != c.link) {
                        out.push((*c).clone());
                        if out.len() == n {
                            break 'outer;
                        }
                    }
                }
            }
        }
        out
    }
}

#[async_trait]
impl Module for NewsModule {
    fn actions(&self) -> Vec<ActionSpec> {
        vec![
            ActionSpec {
                name: "show_news".into(),
                description: "Show the latest news headlines on the screen. Indian news goes in the main area, \
                              international news in the side column. Also starts live TV if none is playing."
                    .into(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "region": { "type": "string", "enum": ["india", "world", "all"],
                                    "description": "Which news to show. Default: all." },
                        "topic": { "type": "string",
                                   "description": "Optional keyword to filter by, e.g. 'cricket' or 'Bengaluru'." }
                    }
                }),
            },
            ActionSpec {
                name: "open_story".into(),
                description: "Open the story with this on-screen number to show its fuller summary.".into(),
                input_schema: json!({
                    "type": "object",
                    "properties": { "number": { "type": "integer", "description": "The story's number on screen." } },
                    "required": ["number"]
                }),
            },
            ActionSpec {
                name: "summarise_story".into(),
                description: "Read aloud a short summary of the story with this on-screen number.".into(),
                input_schema: json!({
                    "type": "object",
                    "properties": { "number": { "type": "integer", "description": "The story's number on screen." } },
                    "required": ["number"]
                }),
            },
        ]
    }

    async fn handle(
        &self,
        action: &Action,
        screen: &ScreenState,
    ) -> Result<ModuleOutput, ActionError> {
        match action.name.as_str() {
            "show_news" => self.show_news(action, screen).await,
            "open_story" | "summarise_story" => {
                let n = action
                    .args
                    .get("number")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0) as u32;
                let card = screen
                    .card(n)
                    .ok_or_else(|| {
                        ActionError::Failed(format!("There's no story number {n} on the screen."))
                    })?
                    .clone();
                if action.name == "open_story" {
                    Ok(ModuleOutput {
                        speech: Some(format!(
                            "{}: {}.",
                            card.source,
                            card.title.trim_end_matches('.')
                        )),
                        updates: vec![PanelUpdate::Show(Panel {
                            slot: Slot::Overlay,
                            content: PanelContent::CardDetail { card },
                        })],
                        follow_up: vec![],
                    })
                } else {
                    let body = if card.summary.is_empty() {
                        &card.title
                    } else {
                        &card.summary
                    };
                    Ok(ModuleOutput {
                        speech: Some(format!("{} reports: {body}", card.source)),
                        ..Default::default()
                    })
                }
            }
            other => Err(ActionError::Unknown(other.into())),
        }
    }
}

impl NewsModule {
    async fn show_news(
        &self,
        action: &Action,
        screen: &ScreenState,
    ) -> Result<ModuleOutput, ActionError> {
        let region = action
            .args
            .get("region")
            .and_then(|v| v.as_str())
            .unwrap_or("all");
        let topic = action
            .args
            .get("topic")
            .and_then(|v| v.as_str())
            .filter(|t| !t.trim().is_empty());
        if self.cache_is_empty().await {
            self.refresh().await;
        }
        if self.cache_is_empty().await {
            return Err(ActionError::Failed(
                "I couldn't get any news just now. Please check the internet connection.".into(),
            ));
        }
        let india = if region == "world" {
            vec![]
        } else {
            self.headlines(Region::India, topic, CARDS_PER_PANEL).await
        };
        let world = if region == "india" {
            vec![]
        } else {
            self.headlines(Region::World, topic, CARDS_PER_PANEL).await
        };
        if india.is_empty() && world.is_empty() {
            return Err(ActionError::Failed(match topic {
                Some(t) => format!("I couldn't find any news about {t} right now."),
                None => {
                    "I couldn't get any news just now. Please check the internet connection.".into()
                }
            }));
        };
        let india_spoken: Vec<Card> = india.iter().take(2).cloned().collect();
        let world_spoken: Vec<Card> = world.iter().take(2).cloned().collect();
        let mut updates = Vec::new();
        if !india.is_empty() {
            updates.push(PanelUpdate::Show(Panel {
                slot: Slot::Secondary,
                content: PanelContent::CardList {
                    title: "India".into(),
                    cards: india,
                },
            }));
        }
        if !world.is_empty() {
            updates.push(PanelUpdate::Show(Panel {
                slot: Slot::Side,
                content: PanelContent::CardList {
                    title: "International".into(),
                    cards: world,
                },
            }));
        }
        let follow_up = if screen.panel(Slot::Primary).is_none() {
            vec![Action::new("play_live_tv", json!({}))]
        } else {
            vec![]
        };
        Ok(ModuleOutput {
            updates,
            speech: Some(briefing(&india_spoken, &world_spoken)),
            follow_up,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::FixtureFetch;

    const A: &str = "https://a.example/rss";
    const B: &str = "https://b.example/rss";
    const W: &str = "https://w.example/rss";
    const BROKEN: &str = "https://broken.example/rss";

    fn src(name: &str, url: &str, region: Region) -> FeedSource {
        FeedSource {
            name: name.into(),
            url: url.into(),
            region,
        }
    }

    fn sources() -> Vec<FeedSource> {
        vec![
            src("Paper A", A, Region::India),
            src("Paper B", B, Region::India),
            src("World Wire", W, Region::World),
            src("Broken", BROKEN, Region::World),
        ]
    }

    fn fixture() -> Arc<FixtureFetch> {
        Arc::new(FixtureFetch::with(&[
            (A, "india_a.xml"),
            (B, "india_b.xml"),
            (W, "world.xml"),
            (BROKEN, "broken.xml"),
        ]))
    }

    fn module(f: Arc<FixtureFetch>) -> NewsModule {
        NewsModule::new(sources(), f)
    }

    fn cards_in(out: &ModuleOutput, slot: Slot) -> Vec<Card> {
        out.updates
            .iter()
            .find_map(|u| match u {
                PanelUpdate::Show(Panel {
                    slot: s,
                    content: PanelContent::CardList { cards, .. },
                }) if *s == slot => Some(cards.clone()),
                _ => None,
            })
            .unwrap_or_default()
    }

    #[test]
    fn parses_a_feed_into_clean_cards() {
        let bytes = std::fs::read(format!(
            "{}/tests/fixtures/india_a.xml",
            env!("CARGO_MANIFEST_DIR")
        ))
        .unwrap();
        let cards = parse_feed(&src("Paper A", A, Region::India), &bytes).unwrap();
        assert_eq!(cards.len(), 3);
        assert_eq!(cards[0].title, "Monsoon reaches Kerala early");
        assert_eq!(cards[0].source, "Paper A");
        assert_eq!(
            cards[0].summary,
            "The southwest monsoon arrived three days early & brought heavy rain."
        );
        assert_eq!(cards[0].link, "https://a.example/monsoon");
        assert!(cards[0].published.is_some());
    }

    #[test]
    fn broken_feed_is_an_error_not_a_panic() {
        assert!(parse_feed(
            &src("Broken", BROKEN, Region::World),
            b"this is <not> a feed"
        )
        .is_err());
    }

    #[tokio::test]
    async fn refresh_reports_failed_sources() {
        let m = module(fixture());
        assert_eq!(m.refresh().await, vec!["Broken".to_string()]);
    }

    #[tokio::test]
    async fn headlines_alternate_sources_and_drop_duplicate_links() {
        let m = module(fixture());
        m.refresh().await;
        let titles: Vec<String> = m
            .headlines(Region::India, None, 10)
            .await
            .into_iter()
            .map(|c| c.title)
            .collect();
        assert_eq!(
            titles,
            vec![
                "Monsoon reaches Kerala early", // A1
                "Parliament session begins",    // B1
                "Bengaluru metro line opens",   // A2 (B2 is a duplicate link)
                "Cricket: India win series",    // A3
            ]
        );
        assert_eq!(m.headlines(Region::India, None, 2).await.len(), 2);
    }

    #[tokio::test]
    async fn topic_filter_is_case_insensitive() {
        let m = module(fixture());
        m.refresh().await;
        let c = m.headlines(Region::India, Some("BENGALURU"), 10).await;
        assert_eq!(c.len(), 1);
        assert_eq!(c[0].title, "Bengaluru metro line opens");
    }

    #[tokio::test]
    async fn a_feed_going_down_keeps_its_last_good_headlines() {
        let f = fixture();
        let m = module(f.clone());
        m.refresh().await;
        f.remove(A);
        let failed = m.refresh().await;
        assert!(failed.contains(&"Paper A".to_string()));
        assert!(m
            .headlines(Region::India, None, 10)
            .await
            .iter()
            .any(|c| c.source == "Paper A"));
    }

    #[tokio::test]
    async fn show_news_fills_india_and_world_and_starts_live_tv() {
        let m = module(fixture());
        let out = m
            .handle(&Action::new("show_news", json!({})), &ScreenState::new())
            .await
            .unwrap();
        assert_eq!(cards_in(&out, Slot::Secondary).len(), 4);
        assert_eq!(cards_in(&out, Slot::Side).len(), 2);
        assert_eq!(out.follow_up, vec![Action::new("play_live_tv", json!({}))]);
        assert_eq!(
            out.speech.unwrap(),
            "Here's the latest. From Paper A: Monsoon reaches Kerala early. \
             From Paper B: Parliament session begins. \
             And internationally, from World Wire: Climate summit agrees new targets. \
             Say a number to hear more."
        );
    }

    #[tokio::test]
    async fn show_news_fetches_lazily_only_when_cache_is_empty() {
        let f = fixture();
        let m = module(f.clone());
        m.handle(&Action::new("show_news", json!({})), &ScreenState::new())
            .await
            .unwrap();
        let calls = f.calls.lock().unwrap().len();
        m.handle(&Action::new("show_news", json!({})), &ScreenState::new())
            .await
            .unwrap();
        assert_eq!(
            f.calls.lock().unwrap().len(),
            calls,
            "second call served from cache"
        );
    }

    #[tokio::test]
    async fn show_news_does_not_restart_tv_already_playing() {
        let m = module(fixture());
        let mut screen = ScreenState::new();
        screen.apply(PanelUpdate::Show(Panel {
            slot: Slot::Primary,
            content: PanelContent::Video {
                title: "x".into(),
                embed_url: "y".into(),
            },
        }));
        let out = m
            .handle(&Action::new("show_news", json!({})), &screen)
            .await
            .unwrap();
        assert!(out.follow_up.is_empty());
    }

    #[tokio::test]
    async fn world_only_briefing_reads_international_headlines() {
        let m = module(fixture());
        let out = m
            .handle(
                &Action::new("show_news", json!({"region": "world"})),
                &ScreenState::new(),
            )
            .await
            .unwrap();
        assert_eq!(
            out.speech.unwrap(),
            "Here's the latest. From World Wire: Climate summit agrees new targets. \
             From World Wire: Markets rally in Asia. \
             Say a number to hear more."
        );
    }

    #[tokio::test]
    async fn show_world_news_only_updates_the_side_column() {
        let m = module(fixture());
        let out = m
            .handle(
                &Action::new("show_news", json!({"region": "world"})),
                &ScreenState::new(),
            )
            .await
            .unwrap();
        assert!(cards_in(&out, Slot::Secondary).is_empty());
        assert_eq!(cards_in(&out, Slot::Side).len(), 2);
    }

    #[tokio::test]
    async fn unmatched_topic_says_so() {
        let m = module(fixture());
        let err = m
            .handle(
                &Action::new("show_news", json!({"topic": "volcano"})),
                &ScreenState::new(),
            )
            .await;
        assert_eq!(
            err,
            Err(ActionError::Failed(
                "I couldn't find any news about volcano right now.".into()
            ))
        );
    }

    #[tokio::test]
    async fn no_internet_at_all_says_so() {
        let m = NewsModule::new(sources(), Arc::new(FixtureFetch::default()));
        let err = m
            .handle(&Action::new("show_news", json!({})), &ScreenState::new())
            .await;
        assert_eq!(
            err,
            Err(ActionError::Failed(
                "I couldn't get any news just now. Please check the internet connection.".into()
            ))
        );
    }

    async fn screen_with_news(m: &NewsModule) -> ScreenState {
        let out = m
            .handle(&Action::new("show_news", json!({})), &ScreenState::new())
            .await
            .unwrap();
        let mut s = ScreenState::new();
        for u in out.updates {
            s.apply(u);
        }
        s
    }

    #[tokio::test]
    async fn open_story_shows_it_as_an_overlay() {
        let m = module(fixture());
        let s = screen_with_news(&m).await;
        let out = m
            .handle(&Action::new("open_story", json!({"number": 2})), &s)
            .await
            .unwrap();
        assert!(matches!(
            &out.updates[0],
            PanelUpdate::Show(Panel { slot: Slot::Overlay, content: PanelContent::CardDetail { card } })
                if card.title == "Parliament session begins"
        ));
    }

    #[tokio::test]
    async fn summarise_story_speaks_its_summary() {
        let m = module(fixture());
        let s = screen_with_news(&m).await;
        let out = m
            .handle(&Action::new("summarise_story", json!({"number": 5})), &s)
            .await
            .unwrap();
        assert_eq!(
            out.speech.unwrap(),
            "World Wire reports: Delegates reached agreement late on Friday."
        );
        assert!(out.updates.is_empty());
    }

    #[tokio::test]
    async fn a_number_not_on_screen_is_explained() {
        let m = module(fixture());
        let s = screen_with_news(&m).await;
        let err = m
            .handle(&Action::new("open_story", json!({"number": 42})), &s)
            .await;
        assert_eq!(
            err,
            Err(ActionError::Failed(
                "There's no story number 42 on the screen.".into()
            ))
        );
    }
}
