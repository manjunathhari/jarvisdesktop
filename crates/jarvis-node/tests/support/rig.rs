//! An enrolled node connected to the fake gateway, for runtime tests.

use std::sync::Arc;
use std::time::Duration;

use jarvis_node::client::ClientConfig;
use jarvis_node::folder::memory::MemoryFolder;
use jarvis_node::keystore::MemoryKeyStore;
use jarvis_node::node::{self, NodeDeps, NodeHandle, UiEvent};
use serde_json::Value;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use uuid::Uuid;

use super::gateway::{code_for, now, Gw};
use super::net::{accept, recv, send, Ws};

pub struct Rig {
    pub listener: TcpListener,
    pub node: NodeHandle,
    pub ui: mpsc::Receiver<UiEvent>,
    pub gw: Gw,
    pub ws: Ws,
    pub node_id: Uuid,
    pub dir: tempfile::TempDir,
    pub folder: Arc<MemoryFolder>,
}

pub fn core_public() -> [u8; 32] {
    super::test_key("core-1").verifying_key().to_bytes()
}

/// An enrolled node, connected to the fake gateway.
pub async fn rig() -> Rig {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let dir = tempfile::tempdir().unwrap();
    let cfg = ClientConfig::new(listener.local_addr().unwrap().to_string(), dir.path());
    let (ui_tx, ui) = mpsc::channel(64);
    let folder = Arc::new(MemoryFolder::new());
    let deps = NodeDeps {
        keystore: Arc::new(MemoryKeyStore::default()),
        ui: ui_tx,
        folder: folder.clone(),
    };
    let node = node::start(cfg, deps).unwrap();
    let mut gw = Gw::new("gw-1", "gw-1");
    let code = code_for(&core_public(), &gw.public());
    let handle = node.clone();
    let enrolment = tokio::spawn(async move { handle.enrol(&code).await });
    let mut ws = accept(&listener).await;
    send(&mut ws, gw.challenge(now())).await;
    assert_eq!(recv(&mut ws).await["kind"], "enrol");
    let node_id = Uuid::new_v4();
    send(
        &mut ws,
        gw.welcome(node_id, Some(("core-1", core_public())), now()),
    )
    .await;
    enrolment.await.unwrap().unwrap();
    Rig {
        listener,
        node,
        ui,
        gw,
        ws,
        node_id,
        dir,
        folder,
    }
}

impl Rig {
    pub async fn push(&mut self, kind: &str, payload: Value) {
        let frame = self.gw.frame(kind, self.node_id, payload, now());
        send(&mut self.ws, frame).await;
    }

    /// The next UI event that isn't a link-status or summary update.
    pub async fn ui_event(&mut self, within: Duration) -> Option<UiEvent> {
        let deadline = tokio::time::Instant::now() + within;
        loop {
            match tokio::time::timeout_at(deadline, self.ui.recv()).await {
                Ok(Some(UiEvent::Link(_) | UiEvent::Summary(_))) => continue,
                Ok(other) => return other,
                Err(_) => return None,
            }
        }
    }
}
