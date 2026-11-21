//! Built-in screen actions: close a panel, clear everything.

use async_trait::async_trait;
use jarvis_core::action::{Action, ActionError, ActionSpec, Module, ModuleOutput};
use jarvis_core::screen::{PanelUpdate, ScreenState, Slot};
use serde_json::json;

pub struct ScreenModule;

#[async_trait]
impl Module for ScreenModule {
    fn actions(&self) -> Vec<ActionSpec> {
        vec![
            ActionSpec {
                name: "close_panel".into(),
                description: "Close one part of the screen: the video, the Indian news, the international news, \
                              or the open story."
                    .into(),
                input_schema: json!({
                    "type": "object",
                    "properties": { "which": { "type": "string", "enum": ["video", "india", "world", "story"] } },
                    "required": ["which"]
                }),
            },
            ActionSpec {
                name: "clear_screen".into(),
                description: "Remove everything from the screen.".into(),
                input_schema: json!({ "type": "object", "properties": {} }),
            },
        ]
    }

    async fn handle(
        &self,
        action: &Action,
        screen: &ScreenState,
    ) -> Result<ModuleOutput, ActionError> {
        match action.name.as_str() {
            "clear_screen" => Ok(ModuleOutput {
                updates: vec![PanelUpdate::Clear],
                ..Default::default()
            }),
            "close_panel" => {
                let which = action
                    .args
                    .get("which")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let (slot, missing) = match which {
                    "video" => (Slot::Primary, "There's no video playing."),
                    "india" => (Slot::Secondary, "The Indian news isn't on the screen."),
                    "world" => (Slot::Side, "The international news isn't on the screen."),
                    "story" => (Slot::Overlay, "There's no story open."),
                    _ => {
                        return Err(ActionError::Failed(
                            "I'm not sure which part to close.".into(),
                        ))
                    }
                };
                if screen.panel(slot).is_none() {
                    return Err(ActionError::Failed(missing.into()));
                }
                Ok(ModuleOutput {
                    updates: vec![PanelUpdate::Remove(slot)],
                    ..Default::default()
                })
            }
            other => Err(ActionError::Unknown(other.into())),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use jarvis_core::screen::{Panel, PanelContent};

    fn with_video() -> ScreenState {
        let mut s = ScreenState::new();
        s.apply(PanelUpdate::Show(Panel {
            slot: Slot::Primary,
            content: PanelContent::Video {
                title: "t".into(),
                embed_url: "u".into(),
            },
        }));
        s
    }

    #[tokio::test]
    async fn close_video_removes_the_primary_panel() {
        let out = ScreenModule
            .handle(
                &Action::new("close_panel", json!({"which": "video"})),
                &with_video(),
            )
            .await
            .unwrap();
        assert_eq!(out.updates, vec![PanelUpdate::Remove(Slot::Primary)]);
    }

    #[tokio::test]
    async fn closing_something_not_shown_is_explained() {
        let err = ScreenModule
            .handle(
                &Action::new("close_panel", json!({"which": "story"})),
                &with_video(),
            )
            .await;
        assert_eq!(
            err,
            Err(ActionError::Failed("There's no story open.".into()))
        );
    }

    #[tokio::test]
    async fn clear_screen_clears() {
        let out = ScreenModule
            .handle(&Action::new("clear_screen", json!({})), &with_video())
            .await
            .unwrap();
        assert_eq!(out.updates, vec![PanelUpdate::Clear]);
    }
}
