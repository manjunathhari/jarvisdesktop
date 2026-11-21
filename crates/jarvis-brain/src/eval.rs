//! Brain evaluation: phrasings → expected action. The yardstick for Claude now
//! and for any local model later (ADR-002).

use jarvis_core::action::Action;
use serde::Deserialize;
use serde_json::Value;

/// The screen every eval case is judged against, so numbered requests make sense.
pub const EVAL_SCREEN: &str = "[primary] video playing: NDTV 24x7 · live
[secondary] India:
  1: The Hindu — Monsoon reaches Kerala early
  2: NDTV — Parliament winter session begins
  3: India Today — Bengaluru metro Yellow Line opens
  4: Times of India — India clinch series 3-1
  5: The Hindu — RBI holds repo rate
  6: NDTV — Heatwave warning for Rajasthan
[side] International:
  7: BBC — Climate summit agrees new targets
  8: Al Jazeera — Ceasefire talks resume
  9: The Guardian — Markets rally in Asia";

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct Case {
    pub say: String,
    /// Expected action name, or "none" when no tool should be called.
    pub expect: String,
    /// Arguments that must be present with these values (others may exist).
    #[serde(default)]
    pub args: toml::Table,
}

#[derive(Debug, Deserialize)]
struct CaseFile {
    case: Vec<Case>,
}

pub fn load_cases(text: &str) -> Result<Vec<Case>, String> {
    toml::from_str::<CaseFile>(text)
        .map(|f| f.case)
        .map_err(|e| e.to_string())
}

fn squash(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_alphanumeric())
        .collect::<String>()
        .to_lowercase()
}

/// Did the brain do the right thing? Judged on its first action.
pub fn passes(case: &Case, actions: &[Action]) -> bool {
    if case.expect == "none" {
        return actions.is_empty();
    }
    let Some(first) = actions.first() else {
        return false;
    };
    if first.name != case.expect {
        return false;
    }
    case.args.iter().all(|(key, want)| {
        let got = first.args.get(key).unwrap_or(&Value::Null);
        match want {
            toml::Value::Integer(n) => got.as_i64() == Some(*n),
            toml::Value::String(w) => got.as_str().is_some_and(|g| {
                let (g, w) = (squash(g), squash(w));
                !g.is_empty() && (g.contains(&w) || w.contains(&g))
            }),
            toml::Value::Boolean(b) => got.as_bool() == Some(*b),
            _ => false,
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn case(expect: &str, args: &str) -> Case {
        Case {
            say: "x".into(),
            expect: expect.into(),
            args: toml::from_str(args).unwrap(),
        }
    }

    #[test]
    fn loads_the_shipped_case_file() {
        let cases = load_cases(include_str!("../../../evals/brain/cases.toml")).unwrap();
        assert!(cases.len() >= 60, "got {}", cases.len());
        for name in [
            "show_news",
            "open_story",
            "summarise_story",
            "play_live_tv",
            "next_channel",
            "close_panel",
            "clear_screen",
            "none",
        ] {
            assert!(
                cases.iter().any(|c| c.expect == name),
                "no cases for {name}"
            );
        }
    }

    #[test]
    fn right_action_and_args_pass() {
        let c = case("open_story", "number = 4");
        assert!(passes(
            &c,
            &[Action::new("open_story", json!({"number": 4}))]
        ));
        assert!(!passes(
            &c,
            &[Action::new("open_story", json!({"number": 3}))]
        ));
        assert!(!passes(
            &c,
            &[Action::new("summarise_story", json!({"number": 4}))]
        ));
        assert!(!passes(&c, &[]));
    }

    #[test]
    fn string_args_compare_case_insensitively_and_extra_args_are_fine() {
        let c = case("show_news", "region = \"india\"");
        assert!(passes(
            &c,
            &[Action::new(
                "show_news",
                json!({"region": "India", "topic": "x"})
            )]
        ));
        assert!(!passes(&c, &[Action::new("show_news", json!({}))]));
    }

    #[test]
    fn expect_none_means_no_tool_call() {
        let c = case("none", "");
        assert!(passes(&c, &[]));
        assert!(!passes(&c, &[Action::new("show_news", json!({}))]));
    }

    #[test]
    fn channel_names_match_loosely() {
        let c = case("play_live_tv", "channel = \"india today\"");
        assert!(passes(
            &c,
            &[Action::new(
                "play_live_tv",
                json!({"channel": "India Today"})
            )]
        ));
        assert!(passes(
            &c,
            &[Action::new(
                "play_live_tv",
                json!({"channel": "IndiaToday"})
            )]
        ));
    }
}
