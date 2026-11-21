//! Live brain evaluation. Needs ANTHROPIC_API_KEY and internet.
//!
//!     cargo run -p jarvis-brain --bin jarvis-eval            # Claude, default model
//!     JARVIS_MODEL=claude-sonnet-5-5 cargo run -p jarvis-brain --bin jarvis-eval
//!
//! Exits non-zero below the 90% gate from the product acceptance criteria.

use futures::StreamExt;
use jarvis_brain::eval::{load_cases, passes, EVAL_SCREEN};
use jarvis_brain::ClaudeBrain;
use jarvis_core::action::{Action, Module};
use jarvis_core::brain::{Brain, BrainEvent, BrainRequest};
use jarvis_modules::{
    FeedSource, HttpFetch, LiveTvModule, NewsModule, ScreenModule, SourcesConfig,
};
use std::collections::BTreeMap;
use std::sync::Arc;
use tokio::sync::mpsc;

const GATE: f64 = 0.90;

#[tokio::main]
async fn main() {
    let key =
        std::env::var("ANTHROPIC_API_KEY").expect("set ANTHROPIC_API_KEY to run the live eval");
    let brain = Arc::new(ClaudeBrain::new(
        key,
        std::env::var("JARVIS_MODEL").ok(),
        "Manj",
    ));

    let cfg = SourcesConfig::default_config();
    let feeds: Vec<FeedSource> = cfg.feeds.clone();
    let modules: Vec<Box<dyn Module>> = vec![
        Box::new(NewsModule::new(feeds, Arc::new(HttpFetch::new()))),
        Box::new(LiveTvModule::new(cfg.channels.clone())),
        Box::new(ScreenModule),
    ];
    let tools: Vec<_> = modules.iter().flat_map(|m| m.actions()).collect();

    let cases = load_cases(include_str!("../../../../evals/brain/cases.toml")).expect("cases");
    let results: Vec<(String, String, bool, Vec<Action>)> = futures::stream::iter(cases)
        .map(|case| {
            let brain = brain.clone();
            let tools = tools.clone();
            async move {
                let (tx, mut rx) = mpsc::channel(32);
                let req = BrainRequest {
                    utterance: case.say.clone(),
                    screen_summary: EVAL_SCREEN.into(),
                    history: vec![],
                    tools,
                };
                let think = tokio::spawn(async move { brain.think(req, tx).await });
                let mut actions = Vec::new();
                while let Some(ev) = rx.recv().await {
                    if let BrainEvent::Action(a) = ev {
                        actions.push(a);
                    }
                }
                if let Ok(Err(e)) = think.await {
                    eprintln!("  ! {:?}: {e}", case.say);
                }
                let ok = passes(&case, &actions);
                (case.expect.clone(), case.say.clone(), ok, actions)
            }
        })
        .buffer_unordered(4)
        .collect()
        .await;

    let mut by_action: BTreeMap<String, (u32, u32)> = BTreeMap::new();
    for (expect, say, ok, actions) in &results {
        let e = by_action.entry(expect.clone()).or_default();
        e.1 += 1;
        if *ok {
            e.0 += 1;
        } else {
            let got: Vec<String> = actions
                .iter()
                .map(|a| format!("{}({})", a.name, a.args))
                .collect();
            println!(
                "FAIL  {say:?}\n      expected {expect}, got [{}]",
                got.join(", ")
            );
        }
    }
    println!("\nPer action:");
    let (mut pass, mut total) = (0, 0);
    for (action, (p, t)) in &by_action {
        println!("  {action:<16} {p}/{t}");
        pass += p;
        total += t;
    }
    let score = pass as f64 / total.max(1) as f64;
    println!(
        "\nOverall: {pass}/{total} = {:.1}% (gate {:.0}%)",
        score * 100.0,
        GATE * 100.0
    );
    if score < GATE {
        std::process::exit(1);
    }
}
