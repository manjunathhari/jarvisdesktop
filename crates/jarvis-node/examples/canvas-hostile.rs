//! The hostile test page for the Canvas seal (fictional; it attacks only its own sandbox).
//!
//! `cargo run -p jarvis-node --example canvas-hostile -- write <dir>` writes a Board store
//! holding one Canvas panel with the page, for the checklist on Windows
//! (`.10x/reviews/2026-11-27-board-canvas-hostile-page.md`).
//!
//! `... -- serve` serves that panel from the real Canvas server and prints its address, for
//! checking the page's own policy in a browser.

use std::sync::{Arc, Mutex};

use jarvis_node::board::{self, BoardStore};
use jarvis_node::canvas;
use serde_json::json;
use uuid::Uuid;

const PAGE: &str = include_str!("hostile.html");

fn store_with_the_page(dir: &std::path::Path) -> (BoardStore, Uuid) {
    let id = Uuid::new_v4();
    let args = json!({
        "panel_id": id, "revision": 1, "kind": "canvas",
        "title": "Canvas seal check (fictional)", "content": PAGE,
        "content_sha256": board::content_sha256(PAGE), "cost_paise": 0,
        "created_at": chrono::Utc::now().to_rfc3339(),
    });
    let event = board::parse("board.show", args.as_object().expect("an object"))
        .expect("the page is a valid panel");
    let mut store = BoardStore::open(dir);
    store.apply(&event).expect("the store takes it");
    (store, id)
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.iter().map(String::as_str).collect::<Vec<_>>()[..] {
        ["write", dir] => {
            std::fs::create_dir_all(dir).expect("the folder");
            let (_, id) = store_with_the_page(std::path::Path::new(dir));
            println!("wrote {dir}/board.json with panel {id}");
        }
        ["serve"] => {
            let dir = std::env::temp_dir().join(format!("canvas-hostile-{}", Uuid::new_v4()));
            std::fs::create_dir_all(&dir).expect("the folder");
            let (store, id) = store_with_the_page(&dir);
            let server = canvas::serve(Arc::new(Mutex::new(store)), &canvas::PORTS)
                .await
                .expect("a canvas port");
            println!("{}", server.url(id, 1));
            std::future::pending::<()>().await;
        }
        _ => eprintln!("usage: canvas-hostile write <dir> | serve"),
    }
}
