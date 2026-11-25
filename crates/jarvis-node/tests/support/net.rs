//! Socket plumbing for tests that run the real driver against the scripted fake gateway.
#![allow(dead_code)]

use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;

pub type Ws = WebSocketStream<TcpStream>;

pub async fn accept(listener: &TcpListener) -> Ws {
    let (tcp, _) = tokio::time::timeout(Duration::from_secs(10), listener.accept())
        .await
        .expect("the node connects")
        .unwrap();
    tokio_tungstenite::accept_async(tcp).await.unwrap()
}

/// The next text frame as JSON (panics after 5 s or if the connection ends).
pub async fn recv(ws: &mut Ws) -> Value {
    recv_within(ws, Duration::from_secs(5))
        .await
        .expect("a frame")
}

/// The next text frame within `within`, or `None` (also when the connection ended).
pub async fn recv_within(ws: &mut Ws, within: Duration) -> Option<Value> {
    let deadline = tokio::time::Instant::now() + within;
    loop {
        match tokio::time::timeout_at(deadline, ws.next()).await {
            Ok(Some(Ok(Message::Text(t)))) => {
                return Some(serde_json::from_str(t.as_str()).unwrap())
            }
            Ok(Some(Ok(Message::Close(_)))) | Ok(None) | Ok(Some(Err(_))) | Err(_) => return None,
            Ok(Some(Ok(_))) => continue,
        }
    }
}

pub async fn send(ws: &mut Ws, frame: String) {
    ws.send(Message::text(frame)).await.unwrap();
}

pub async fn close(ws: &mut Ws, code: u16) {
    let _ = ws
        .send(Message::Close(Some(CloseFrame {
            code: CloseCode::from(code),
            reason: "test".into(),
        })))
        .await;
}
