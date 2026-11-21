//! Turns the Messages API stream into `BrainEvent`s: spoken text in
//! sentence-sized pieces (so the voice can start early) and complete tool calls.

use crate::sse::SseEvent;
use jarvis_core::action::Action;
use jarvis_core::brain::{BrainError, BrainEvent};
use serde_json::Value;
use std::collections::HashMap;

#[derive(Debug, Default)]
pub struct Assembler {
    text: String,
    tools: HashMap<u64, (String, String)>,
    pub finished: bool,
}

impl Assembler {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn on_event(&mut self, ev: &SseEvent) -> Result<Vec<BrainEvent>, BrainError> {
        let v: Value = match serde_json::from_str(&ev.data) {
            Ok(v) => v,
            Err(_) if ev.event == "ping" => return Ok(vec![]),
            Err(e) => return Err(BrainError::Protocol(format!("bad event data: {e}"))),
        };
        let index = v.get("index").and_then(Value::as_u64).unwrap_or(0);
        let mut out = Vec::new();
        match v
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or(ev.event.as_str())
        {
            "content_block_start" => {
                let block = &v["content_block"];
                if block["type"] == "tool_use" {
                    let name = block["name"].as_str().unwrap_or_default().to_string();
                    self.tools.insert(index, (name, String::new()));
                }
            }
            "content_block_delta" => {
                let d = &v["delta"];
                match d["type"].as_str() {
                    Some("text_delta") => {
                        self.text.push_str(d["text"].as_str().unwrap_or_default());
                        out.extend(
                            take_sentences(&mut self.text)
                                .into_iter()
                                .map(BrainEvent::Speech),
                        );
                    }
                    Some("input_json_delta") => {
                        if let Some((_, json)) = self.tools.get_mut(&index) {
                            json.push_str(d["partial_json"].as_str().unwrap_or_default());
                        }
                    }
                    _ => {}
                }
            }
            "content_block_stop" => match self.tools.remove(&index) {
                Some((name, json)) => {
                    let args: Value = if json.trim().is_empty() {
                        Value::Object(Default::default())
                    } else {
                        serde_json::from_str(&json).map_err(|e| {
                            BrainError::Protocol(format!("bad tool input for {name}: {e}"))
                        })?
                    };
                    out.push(BrainEvent::Action(Action { name, args }));
                }
                None => out.extend(self.flush()),
            },
            "message_stop" => self.finished = true,
            "error" => {
                let msg = v["error"]["message"].as_str().unwrap_or("unknown error");
                return Err(BrainError::Unavailable(msg.to_string()));
            }
            _ => {}
        }
        Ok(out)
    }

    /// Whatever text is left once the stream ends.
    pub fn flush(&mut self) -> Vec<BrainEvent> {
        let rest = std::mem::take(&mut self.text);
        let rest = rest.trim();
        if rest.is_empty() {
            vec![]
        } else {
            vec![BrainEvent::Speech(rest.to_string())]
        }
    }
}

/// Split complete sentences off the front of `buf`.
fn take_sentences(buf: &mut String) -> Vec<String> {
    let mut out = Vec::new();
    loop {
        let chars: Vec<(usize, char)> = buf.char_indices().collect();
        let cut = chars.windows(2).find_map(|w| {
            let ((_, c), (j, next)) = (w[0], w[1]);
            (matches!(c, '.' | '!' | '?') && next.is_whitespace()).then_some(j)
        });
        match cut {
            Some(j) => {
                let sentence = buf[..j].trim().to_string();
                let rest = buf[j..].trim_start().to_string();
                *buf = rest;
                if !sentence.is_empty() {
                    out.push(sentence);
                }
            }
            None => return out,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ev(event: &str, data: serde_json::Value) -> SseEvent {
        SseEvent {
            event: event.into(),
            data: data.to_string(),
        }
    }

    fn run(events: Vec<SseEvent>) -> Result<Vec<BrainEvent>, BrainError> {
        let mut a = Assembler::new();
        let mut out = Vec::new();
        for e in &events {
            out.extend(a.on_event(e)?);
        }
        out.extend(a.flush());
        Ok(out)
    }

    #[test]
    fn text_is_spoken_sentence_by_sentence() {
        let out = run(vec![
            ev("content_block_start", json!({"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}})),
            ev("content_block_delta", json!({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Certainly. Here is "}})),
            ev("content_block_delta", json!({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"the news! Anything"}})),
            ev("content_block_delta", json!({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" else"}})),
            ev("content_block_stop", json!({"type":"content_block_stop","index":0})),
            ev("message_stop", json!({"type":"message_stop"})),
        ])
        .unwrap();
        assert_eq!(
            out,
            vec![
                BrainEvent::Speech("Certainly.".into()),
                BrainEvent::Speech("Here is the news!".into()),
                BrainEvent::Speech("Anything else".into()),
            ]
        );
    }

    #[test]
    fn tool_calls_are_assembled_from_partial_json() {
        let out = run(vec![
            ev("content_block_start", json!({"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t1","name":"open_story","input":{}}})),
            ev("content_block_delta", json!({"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"num"}})),
            ev("content_block_delta", json!({"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"ber\": 4}"}})),
            ev("content_block_stop", json!({"type":"content_block_stop","index":1})),
        ])
        .unwrap();
        assert_eq!(
            out,
            vec![BrainEvent::Action(Action::new(
                "open_story",
                json!({"number": 4})
            ))]
        );
    }

    #[test]
    fn tool_call_with_no_arguments_gets_an_empty_object() {
        let out = run(vec![
            ev("content_block_start", json!({"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t","name":"show_news","input":{}}})),
            ev("content_block_stop", json!({"type":"content_block_stop","index":0})),
        ])
        .unwrap();
        assert_eq!(
            out,
            vec![BrainEvent::Action(Action::new("show_news", json!({})))]
        );
    }

    #[test]
    fn stream_error_event_is_an_error() {
        let r = run(vec![ev(
            "error",
            json!({"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}),
        )]);
        assert!(matches!(r, Err(BrainError::Unavailable(m)) if m.contains("Overloaded")));
    }

    #[test]
    fn message_stop_marks_finished_and_pings_are_ignored() {
        let mut a = Assembler::new();
        assert!(a
            .on_event(&ev("ping", json!({"type":"ping"})))
            .unwrap()
            .is_empty());
        a.on_event(&ev("message_stop", json!({"type":"message_stop"})))
            .unwrap();
        assert!(a.finished);
    }

    #[test]
    fn decimals_and_abbreviations_do_not_split_mid_number() {
        let mut b = "It rose 3.5 percent. Next".to_string();
        assert_eq!(take_sentences(&mut b), vec!["It rose 3.5 percent."]);
        assert_eq!(b, "Next");
    }
}
