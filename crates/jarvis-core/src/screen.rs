//! What is on the screen. Rust owns this; the UI only renders a `ScreenView`.
//!
//! Every card in a list panel gets a number, in reading order
//! (Primary → Secondary → Side), so the user can say "open number 4".

use serde::{Deserialize, Serialize};

/// Where a panel sits on the wall.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum Slot {
    /// Big area, top-left (live video).
    Primary,
    /// Below the primary area (main headlines).
    Secondary,
    /// Right-hand column (international headlines).
    Side,
    /// Shown over everything (an opened story).
    Overlay,
}

/// One story / item shown as a card.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Card {
    pub title: String,
    pub source: String,
    pub summary: String,
    pub link: String,
    /// RFC 3339 timestamp if known.
    pub published: Option<String>,
}

/// The fixed vocabulary of panel kinds (ADR-003). Modules never ship HTML.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum PanelContent {
    CardList { title: String, cards: Vec<Card> },
    CardDetail { card: Card },
    Video { title: String, embed_url: String },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Panel {
    pub slot: Slot,
    pub content: PanelContent,
}

/// A change a module asks for.
#[derive(Debug, Clone, PartialEq)]
pub enum PanelUpdate {
    /// Put this panel in its slot, replacing whatever was there.
    Show(Panel),
    /// Remove the panel in this slot, if any.
    Remove(Slot),
    /// Empty the screen.
    Clear,
}

/// A card as rendered, carrying its spoken number.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NumberedCard {
    pub number: u32,
    #[serde(flatten)]
    pub card: Card,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum PanelView {
    CardList {
        title: String,
        cards: Vec<NumberedCard>,
    },
    CardDetail {
        card: Card,
    },
    Video {
        title: String,
        embed_url: String,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SlotView {
    pub slot: Slot,
    #[serde(flatten)]
    pub panel: PanelView,
}

/// What the UI receives.
#[derive(Debug, Clone, PartialEq, Serialize, Default)]
pub struct ScreenView {
    pub panels: Vec<SlotView>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct ScreenState {
    panels: Vec<Panel>,
}

impl ScreenState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn apply(&mut self, update: PanelUpdate) {
        match update {
            PanelUpdate::Show(panel) => {
                self.panels.retain(|p| p.slot != panel.slot);
                self.panels.push(panel);
                self.panels.sort_by_key(|p| p.slot);
            }
            PanelUpdate::Remove(slot) => self.panels.retain(|p| p.slot != slot),
            PanelUpdate::Clear => self.panels.clear(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.panels.is_empty()
    }

    pub fn panel(&self, slot: Slot) -> Option<&Panel> {
        self.panels.iter().find(|p| p.slot == slot)
    }

    /// Every numbered card, in reading order, numbered from 1.
    pub fn numbered_cards(&self) -> Vec<(u32, &Card)> {
        let mut out = Vec::new();
        let mut n = 0;
        for p in &self.panels {
            if let PanelContent::CardList { cards, .. } = &p.content {
                for c in cards {
                    n += 1;
                    out.push((n, c));
                }
            }
        }
        out
    }

    /// The card the user calls "number `n`".
    pub fn card(&self, number: u32) -> Option<&Card> {
        self.numbered_cards()
            .into_iter()
            .find(|(n, _)| *n == number)
            .map(|(_, c)| c)
    }

    /// Compact text description for the brain, so it knows what "number 4" means.
    pub fn summary(&self) -> String {
        if self.panels.is_empty() {
            return "The screen is empty.".to_string();
        }
        let numbered = self.numbered_cards();
        let mut lines = Vec::new();
        let mut idx = 0;
        for p in &self.panels {
            let slot = format!("{:?}", p.slot).to_lowercase();
            match &p.content {
                PanelContent::Video { title, .. } => {
                    lines.push(format!("[{slot}] video playing: {title}"))
                }
                PanelContent::CardDetail { card } => lines.push(format!(
                    "[{slot}] story open: {} ({})",
                    card.title, card.source
                )),
                PanelContent::CardList { title, cards } => {
                    lines.push(format!("[{slot}] {title}:"));
                    for _ in cards {
                        let (n, c) = numbered[idx];
                        idx += 1;
                        lines.push(format!("  {n}: {} — {}", c.source, c.title));
                    }
                }
            }
        }
        lines.join("\n")
    }

    pub fn view(&self) -> ScreenView {
        let numbered = self.numbered_cards();
        let mut idx = 0;
        let panels = self
            .panels
            .iter()
            .map(|p| {
                let panel = match &p.content {
                    PanelContent::CardList { title, cards } => {
                        let cards = cards
                            .iter()
                            .map(|_| {
                                let (number, card) = numbered[idx];
                                idx += 1;
                                NumberedCard {
                                    number,
                                    card: card.clone(),
                                }
                            })
                            .collect();
                        PanelView::CardList {
                            title: title.clone(),
                            cards,
                        }
                    }
                    PanelContent::CardDetail { card } => {
                        PanelView::CardDetail { card: card.clone() }
                    }
                    PanelContent::Video { title, embed_url } => PanelView::Video {
                        title: title.clone(),
                        embed_url: embed_url.clone(),
                    },
                };
                SlotView {
                    slot: p.slot,
                    panel,
                }
            })
            .collect();
        ScreenView { panels }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub fn card(title: &str, source: &str) -> Card {
        Card {
            title: title.into(),
            source: source.into(),
            summary: format!("Summary of {title}"),
            link: format!("https://example.com/{}", title.replace(' ', "-")),
            published: None,
        }
    }

    pub fn list(slot: Slot, title: &str, cards: Vec<Card>) -> PanelUpdate {
        PanelUpdate::Show(Panel {
            slot,
            content: PanelContent::CardList {
                title: title.into(),
                cards,
            },
        })
    }

    #[test]
    fn numbers_cards_in_reading_order_across_panels() {
        let mut s = ScreenState::new();
        // Inserted side first, to prove ordering is by slot, not insertion.
        s.apply(list(
            Slot::Side,
            "World",
            vec![card("W1", "BBC"), card("W2", "Al Jazeera")],
        ));
        s.apply(list(
            Slot::Secondary,
            "India",
            vec![card("I1", "The Hindu"), card("I2", "NDTV")],
        ));

        assert_eq!(s.card(1).unwrap().title, "I1");
        assert_eq!(s.card(2).unwrap().title, "I2");
        assert_eq!(s.card(3).unwrap().title, "W1");
        assert_eq!(s.card(4).unwrap().title, "W2");
        assert!(s.card(5).is_none());
        assert!(s.card(0).is_none());
    }

    #[test]
    fn showing_a_panel_replaces_the_one_in_the_same_slot() {
        let mut s = ScreenState::new();
        s.apply(list(Slot::Side, "World", vec![card("Old", "BBC")]));
        s.apply(list(Slot::Side, "World", vec![card("New", "BBC")]));
        assert_eq!(s.numbered_cards().len(), 1);
        assert_eq!(s.card(1).unwrap().title, "New");
    }

    #[test]
    fn remove_and_clear() {
        let mut s = ScreenState::new();
        s.apply(list(Slot::Side, "World", vec![card("W1", "BBC")]));
        s.apply(list(Slot::Secondary, "India", vec![card("I1", "NDTV")]));
        s.apply(PanelUpdate::Remove(Slot::Secondary));
        assert_eq!(
            s.card(1).unwrap().title,
            "W1",
            "numbers close up after removal"
        );
        s.apply(PanelUpdate::Clear);
        assert!(s.is_empty());
    }

    #[test]
    fn summary_tells_the_brain_what_each_number_is() {
        let mut s = ScreenState::new();
        assert_eq!(s.summary(), "The screen is empty.");
        s.apply(PanelUpdate::Show(Panel {
            slot: Slot::Primary,
            content: PanelContent::Video {
                title: "NDTV 24x7 live".into(),
                embed_url: "u".into(),
            },
        }));
        s.apply(list(
            Slot::Secondary,
            "India",
            vec![card("Monsoon update", "The Hindu")],
        ));
        let sum = s.summary();
        assert!(sum.contains("video playing: NDTV 24x7 live"), "{sum}");
        assert!(sum.contains("1: The Hindu — Monsoon update"), "{sum}");
    }

    #[test]
    fn view_carries_numbers_for_the_ui() {
        let mut s = ScreenState::new();
        s.apply(list(
            Slot::Secondary,
            "India",
            vec![card("A", "X"), card("B", "Y")],
        ));
        let v = s.view();
        let json = serde_json::to_value(&v).unwrap();
        assert_eq!(json["panels"][0]["slot"], "secondary");
        assert_eq!(json["panels"][0]["kind"], "card_list");
        assert_eq!(json["panels"][0]["cards"][1]["number"], 2);
        assert_eq!(json["panels"][0]["cards"][1]["title"], "B");
    }
}
