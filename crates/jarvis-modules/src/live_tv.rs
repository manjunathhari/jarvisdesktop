//! Live TV module: official YouTube live streams in the primary slot.

use crate::config::Channel;
use async_trait::async_trait;
use jarvis_core::action::{Action, ActionError, ActionSpec, Module, ModuleOutput};
use jarvis_core::screen::{Panel, PanelContent, PanelUpdate, ScreenState, Slot};
use serde_json::json;
use std::sync::Mutex;

pub fn embed_url(channel_id: &str) -> String {
    format!("https://www.youtube.com/embed/live_stream?channel={channel_id}&autoplay=1")
}

fn squash(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_alphanumeric())
        .collect::<String>()
        .to_lowercase()
}

pub struct LiveTvModule {
    channels: Vec<Channel>,
    current: Mutex<Option<usize>>,
}

impl LiveTvModule {
    pub fn new(channels: Vec<Channel>) -> Self {
        Self {
            channels,
            current: Mutex::new(None),
        }
    }
}

#[async_trait]
impl Module for LiveTvModule {
    fn actions(&self) -> Vec<ActionSpec> {
        let names: Vec<&str> = self.channels.iter().map(|c| c.name.as_str()).collect();
        vec![
            ActionSpec {
                name: "play_live_tv".into(),
                description: format!(
                    "Play a live news channel in the video panel. Available channels: {}. \
                     Leave `channel` out for the default.",
                    names.join(", ")
                ),
                input_schema: json!({
                    "type": "object",
                    "properties": { "channel": { "type": "string", "description": "Channel name, e.g. 'India Today'." } }
                }),
            },
            ActionSpec {
                name: "next_channel".into(),
                description: "Switch the live video to the next channel on the list.".into(),
                input_schema: json!({ "type": "object", "properties": {} }),
            },
        ]
    }

    async fn handle(
        &self,
        action: &Action,
        screen: &ScreenState,
    ) -> Result<ModuleOutput, ActionError> {
        let _ = screen;
        if self.channels.is_empty() {
            return Err(ActionError::Failed(
                "I don't have any live channels set up.".into(),
            ));
        }
        let mut current = self.current.lock().expect("live tv lock");
        let (index, speech) = match action.name.as_str() {
            "play_live_tv" => match action.args.get("channel").and_then(|v| v.as_str()) {
                None => (0, None),
                Some(wanted) => {
                    let w = squash(wanted);
                    let found = self.channels.iter().position(|c| {
                        let n = squash(&c.name);
                        !w.is_empty() && (n.contains(&w) || w.contains(&n))
                    });
                    match found {
                        Some(i) => (i, Some(format!("Playing {} live.", self.channels[i].name))),
                        None => {
                            let names: Vec<&str> =
                                self.channels.iter().map(|c| c.name.as_str()).collect();
                            let list = match names.split_last() {
                                Some((last, rest)) if !rest.is_empty() => {
                                    format!("{} or {last}", rest.join(", "))
                                }
                                _ => names.join(""),
                            };
                            return Err(ActionError::Failed(format!(
                                "I don't have {wanted} on my channel list. I can play {list}."
                            )));
                        }
                    }
                }
            },
            "next_channel" => {
                let i = current.map(|i| (i + 1) % self.channels.len()).unwrap_or(0);
                (i, Some(format!("Switching to {}.", self.channels[i].name)))
            }
            other => return Err(ActionError::Unknown(other.into())),
        };
        *current = Some(index);
        let ch = &self.channels[index];
        Ok(ModuleOutput {
            updates: vec![PanelUpdate::Show(Panel {
                slot: Slot::Primary,
                content: PanelContent::Video {
                    title: format!("{} · live", ch.name),
                    embed_url: embed_url(&ch.youtube_channel_id),
                },
            })],
            speech,
            follow_up: vec![],
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tv() -> LiveTvModule {
        LiveTvModule::new(vec![
            Channel {
                name: "NDTV 24x7".into(),
                youtube_channel_id: "UC_ndtv".into(),
            },
            Channel {
                name: "India Today".into(),
                youtube_channel_id: "UC_it".into(),
            },
            Channel {
                name: "WION".into(),
                youtube_channel_id: "UC_wion".into(),
            },
        ])
    }

    fn video_of(out: &ModuleOutput) -> (String, String) {
        match &out.updates[0] {
            PanelUpdate::Show(Panel {
                slot: Slot::Primary,
                content: PanelContent::Video { title, embed_url },
            }) => (title.clone(), embed_url.clone()),
            other => panic!("expected primary video, got {other:?}"),
        }
    }

    #[test]
    fn embed_url_uses_the_channel_live_stream() {
        assert_eq!(
            embed_url("UC123"),
            "https://www.youtube.com/embed/live_stream?channel=UC123&autoplay=1"
        );
    }

    #[tokio::test]
    async fn plays_the_default_channel_quietly_when_none_named() {
        let out = tv()
            .handle(&Action::new("play_live_tv", json!({})), &ScreenState::new())
            .await
            .unwrap();
        let (title, url) = video_of(&out);
        assert_eq!(title, "NDTV 24x7 · live");
        assert!(url.contains("UC_ndtv"));
        assert_eq!(
            out.speech, None,
            "no extra chatter when started alongside the news"
        );
    }

    #[tokio::test]
    async fn finds_a_named_channel_loosely() {
        let out = tv()
            .handle(
                &Action::new("play_live_tv", json!({"channel": "india today"})),
                &ScreenState::new(),
            )
            .await
            .unwrap();
        assert!(video_of(&out).1.contains("UC_it"));
        assert_eq!(out.speech.as_deref(), Some("Playing India Today live."));
        let out = tv()
            .handle(
                &Action::new("play_live_tv", json!({"channel": "IndiaToday"})),
                &ScreenState::new(),
            )
            .await
            .unwrap();
        assert!(video_of(&out).1.contains("UC_it"), "ignores spaces");
    }

    #[tokio::test]
    async fn unknown_channel_lists_what_is_available() {
        let err = tv()
            .handle(
                &Action::new("play_live_tv", json!({"channel": "CNN"})),
                &ScreenState::new(),
            )
            .await;
        assert_eq!(
            err,
            Err(ActionError::Failed(
                "I don't have CNN on my channel list. I can play NDTV 24x7, India Today or WION."
                    .into()
            ))
        );
    }

    #[tokio::test]
    async fn next_channel_cycles_and_wraps() {
        let m = tv();
        let s = ScreenState::new();
        m.handle(&Action::new("play_live_tv", json!({"channel": "WION"})), &s)
            .await
            .unwrap();
        let out = m
            .handle(&Action::new("next_channel", json!({})), &s)
            .await
            .unwrap();
        assert!(video_of(&out).1.contains("UC_ndtv"), "wraps to the first");
        assert_eq!(out.speech.as_deref(), Some("Switching to NDTV 24x7."));
        let out = m
            .handle(&Action::new("next_channel", json!({})), &s)
            .await
            .unwrap();
        assert!(video_of(&out).1.contains("UC_it"));
    }
}
